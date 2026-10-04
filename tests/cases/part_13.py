"""Проверки движка Aurora, часть 13 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap

from harness import (  # noqa: F401
    ui_source,
    KIT,
    SCRIPTS,
    TEMPLATE_PAR,
    TEMPLATE_PAR2,
    TEMPLATE_ROW,
    _FakeReader,
    _answer,
    _cockpit_on,
    _review_checklist,
    _review_module,
    _template_mirror,
    card,
    card_srcs,
    make_project,
    panel_sources,
    run,
    set_home,
    stub_messages,
    test,
)


@test
def test_the_kit_updates_itself_from_an_archive_install(tmp: Path):
    """Кит, скачанный архивом, обновляется из панели одной кнопкой — без git и ручных архивов.

    Просьба пользователя 23.09.2026: коллега скачал Aurora архивом с GitHub, и кнопка
    обновления отвечала ему «kit не под git». Теперь кит узнаёт версию по VERSION на
    GitHub, скачивает архив и заменяет файлы поставки; личное не трогает, заменённое
    копирует, устаревшее из прошлой поставки убирает, во время прогона не обновляется.
    """
    import io
    import zipfile
    kit = tmp / "aurora-studio"
    for rel, body in {"VERSION": "1.0.0\n", "scripts/a.py": "old\n", "scripts/gone.py": "x\n",
                      "local/private_terms.txt": "СЕКРЕТ\n", ".env.test": "TOKEN=1\n",
                      "mine.txt": "моё\n"}.items():
        (kit / rel).parent.mkdir(parents=True, exist_ok=True)
        (kit / rel).write_text(body, encoding="utf-8")
    (kit / ".aurora-install.json").write_text(json.dumps(
        {"version": "1.0.0", "files": ["VERSION", "scripts/a.py", "scripts/gone.py"]}), encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("aurora-studio-master/VERSION", "1.1.0\n")
        z.writestr("aurora-studio-master/scripts/a.py", "new\n")
        info = zipfile.ZipInfo("aurora-studio-master/scripts/new.sh")
        info.external_attr = (0o100755 << 16)
        z.writestr(info, "#!/bin/sh\n")
        z.writestr("aurora-studio-master/CHANGELOG.md", "# CHANGELOG\n")
    changelog = "# CHANGELOG\n\n## 1.1.0 — обновление одной кнопкой\n\n## 1.0.0 — начало\n"
    ck, restore = _cockpit_on(kit)

    def fake_get(url, limit, timeout=20):
        if url.endswith("/VERSION"):
            return b"1.1.0\n"
        if url.endswith("/CHANGELOG.md"):
            return changelog.encode("utf-8")
        assert "codeload.github.com/VVG-web/aurora-studio/zip/refs/heads/master" in url, url
        return buf.getvalue()

    ck._http_get = fake_get
    try:
        st = ck.kit_update_status(fresh=True)
        assert st["mode"] == "archive" and st["newer"] and st["latest"] == "1.1.0", st
        assert st["notes"] == ["1.1.0 — обновление одной кнопкой"], st["notes"]
        (kit / "running.json").write_text(json.dumps({"j": {"cmd": "agent:distill"}}), encoding="utf-8")
        busy = ck.kit_update()
        assert "agent:distill" in busy.get("error", ""), f"обновление пошло во время прогона: {busy}"
        (kit / "running.json").write_text("{}", encoding="utf-8")
        r = ck.kit_update()
    finally:
        restore()
    assert r.get("ok") and r["from"] == "1.0.0" and r["to"] == "1.1.0" and r["restart"], r
    assert (kit / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    assert (kit / "scripts/a.py").read_text(encoding="utf-8") == "new\n", "файл поставки не заменён"
    assert os.access(kit / "scripts/new.sh", os.X_OK), "исполняемый файл потерял права"
    assert not (kit / "scripts/gone.py").exists(), "устаревший файл прошлой поставки остался"
    assert (kit / "local/private_terms.txt").read_text(encoding="utf-8") == "СЕКРЕТ\n", "тронуто личное"
    assert (kit / ".env.test").read_text(encoding="utf-8") == "TOKEN=1\n", "тронуты личные настройки"
    assert (kit / "mine.txt").exists(), "убран чужой файл, которого не было в прошлой поставке"
    backup = Path(r["backup"])
    assert (backup / "scripts/a.py").read_text(encoding="utf-8") == "old\n" and (backup / "scripts/gone.py").exists(), \
        "заменённое и убранное не сохранены копией"
    assert "scripts/new.sh" in json.loads((kit / ".aurora-install.json").read_text(encoding="utf-8"))["files"]


@test
def test_the_kit_updates_itself_as_a_clone_even_after_a_history_rewrite(tmp: Path):
    """Клон обновляется из панели и тогда, когда историю на GitHub переписали.

    23.09.2026 историю публичного репозитория переписали (убирали внутреннее название), и
    у каждого клона она «разошлась»: прежняя кнопка (`pull --ff-only`) отказывала.
    Коммиты, пришедшие с GitHub, не своя работа — их сохраняем в запасную ветку и встаём на
    новую историю. Свои коммиты кнопкой не трогаем никогда.
    """
    def git(cwd, *a):
        r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                           cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, (a, r.stderr)
        return r.stdout.strip()

    up = tmp / "upstream"
    up.mkdir()
    git(up, "init", "-q", "-b", "master")
    (up / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (up / "CHANGELOG.md").write_text("## 1.0.0 — начало\n", encoding="utf-8")
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", "1.0.0")
    kit = tmp / "kit"
    git(tmp, "clone", "-q", str(up), str(kit))
    (up / "VERSION").write_text("1.1.0\n", encoding="utf-8")
    (up / "CHANGELOG.md").write_text("## 1.1.0 — вперёд\n\n## 1.0.0 — начало\n", encoding="utf-8")
    git(up, "commit", "-q", "-am", "1.1.0")
    ck, restore = _cockpit_on(kit)
    try:
        r = ck.kit_update()
        assert r.get("ok") and r["to"] == "1.1.0" and r["how"] == "git", r
        # история на GitHub переписана: тот же 1.1.0 другим коммитом, поверх — 1.2.0
        git(up, "commit", "-q", "--amend", "-m", "1.1.0 (переписан)")
        (up / "VERSION").write_text("1.2.0\n", encoding="utf-8")
        git(up, "commit", "-q", "-am", "1.2.0")
        r = ck.kit_update()
        assert r.get("ok") and r["to"] == "1.2.0", r
        assert any("aurora-backup/1.1.0-" in n for n in r["notes"]), r["notes"]
        assert "aurora-backup/1.1.0-" in git(kit, "branch", "--list", "aurora-backup/*")
        # свой коммит в ките — кнопка отказывается, ничего не трогая
        (up / "VERSION").write_text("1.3.0\n", encoding="utf-8")
        git(up, "commit", "-q", "--amend", "-am", "1.3.0, история снова переписана")
        (kit / "mine.txt").write_text("своё\n", encoding="utf-8")
        git(kit, "add", "mine.txt")
        git(kit, "commit", "-q", "-m", "своя правка")
        head = git(kit, "rev-parse", "HEAD")
        r = ck.kit_update()
        assert "свои коммиты" in r.get("error", ""), r
        assert git(kit, "rev-parse", "HEAD") == head, "свой коммит потерян"
    finally:
        restore()


@test
def test_the_panel_offers_the_kit_update_itself(tmp: Path):
    """Панель сама говорит о новой версии и после обновления перезапускается без человека."""
    ui = panel_sources()
    boot = ui[ui.index("async function boot("):]
    assert "checkKitUpdate()" in boot[:4000], "панель не проверяет новую версию при старте"
    check = ui[ui.index("async function checkKitUpdate("):]
    assert 'setBadge("about"' in check and "aurora-kit-told" in check and "aurora-kit-updated" in check, \
        "новая версия не отмечена в меню, или напоминание повторяется, или итог обновления теряется"
    ctx = ui[ui.index("function moduleCtx("):ui.index("function moduleCtx(") + 1200]
    assert "restartPanel" in ctx, "раздел не может перезапустить панель после обновления"
    about = (KIT / "cockpit/modules/about/view.js").read_text(encoding="utf-8")
    assert '"/api/kit/update"' in about and "ctx.restartPanel(" in about, \
        "после обновления панель не перезапускается сама"
    for word in ("branch_is", "ahead", "dirty", "incoming"):
        assert f"about.{word}" not in about, f"человеку снова показывают git: about.{word}"
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "kit_update_status(fresh=" in src and "kit_update()" in src, "маршруты не ведут в новое обновление"
    assert ".aurora-install.json" in (KIT / ".gitignore").read_text(encoding="utf-8")
    # Первый старт новой версии собирает реестр команд — на нагруженной машине полторы
    # минуты (замер 23.09.2026). Страница ждёт по лёгкому `/api/ping` и говорит, чего ждёт;
    # новая панель собирает реестр сразу, в фоне и один раз.
    restart = ui[ui.index("async function restartPanel("):ui.index("async function checkKitUpdate(")]
    assert '"/api/ping' in restart and "p.ready" in restart and "opts.patient" in restart, \
        "после обновления страница ждёт по тяжёлому /api/state или сдаётся через 20 секунд"
    assert "patient: true" in about, "обновление перезапускает панель без терпения к первому старту"
    assert "threading.Thread(target=registry" in src and "_REGISTRY_LOCK" in src, \
        "реестр собирается на первом запросе страницы, а не сразу и не один раз"
    assert '"/api/ping"' in src and "registry_ready()" in src


@test
def test_request_context_reads_mentions_attachments_and_never_secrets(tmp: Path):
    """Контекст запроса: `@путь`, вложения, `/навык`, `@MCP` — и ни одного секрета.

    Просьба пользователя 24.09.2026: в «Продуктивности» ссылаться на файл или папку проекта
    через `@`, прикладывать внешний текстовый файл, звать навык (`/grill-me`) и MCP-сервер
    прямо из текста задачи. Вложения живут в `.opencode/context/<день>/`.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    RC = importlib.import_module("request_context")
    root = tmp / "проект"
    (root / "Requirements").mkdir(parents=True)
    (root / "Requirements" / "Истории пользователей.md").write_text("US-1: вход по паролю\n", encoding="utf-8")
    (root / "Requirements" / "US-2.md").write_text("US-2: выход\n", encoding="utf-8")
    (root / ".env.aurora.test").write_text("TOKEN=секрет\n", encoding="utf-8")
    (root / "local").mkdir()
    (root / "local" / "mcp.json").write_text('{"k": "секрет"}', encoding="utf-8")

    # вложение: текст — да, двоичное, секрет и чужое расширение — нет
    ok = RC.save_attachment(str(root), "Постановка.md", "Нужна кнопка «Выгрузить»".encode())
    assert ok.get("path", "").startswith(".opencode/context/") and (root / ok["path"]).is_file(), ok
    assert "error" in RC.save_attachment(str(root), "a.png", b"\x89PNG\x00\x00")
    assert "error" in RC.save_attachment(str(root), "x.txt", b"\x00\x01\x02")
    assert "error" in RC.save_attachment(str(root), ".env", b"A=1")
    # старые папки вложений чистятся
    old = root / ".opencode" / "context" / "2020-01-01"
    old.mkdir(parents=True)
    assert RC.trim_context(str(root)) == 1 and not old.exists()

    block, notes = RC.read_context(str(root), ["Requirements", ok["path"], ".env.aurora.test",
                                               "local/mcp.json", "../чужое"])
    assert "US-1: вход по паролю" in block and "US-2: выход" in block, "папка не прочитана"
    assert "Нужна кнопка" in block, "вложение не прочитано"
    assert "секрет" not in block, "секрет попал в задание модели"
    assert any("секреты" in n for n in notes) and any("чужое" in n for n in notes), notes

    # навыки: проект → кит → ~/.claude/skills; вложенный навык — на один уровень
    home = tmp / "home"
    (home / ".claude" / "skills" / "grill-me").mkdir(parents=True)
    (home / ".claude" / "skills" / "grill-me" / "SKILL.md").write_text(
        "---\nname: grill-me\n---\nRun a /grilling session.\n", encoding="utf-8")
    (home / ".claude" / "skills" / "grilling").mkdir(parents=True)
    (home / ".claude" / "skills" / "grilling" / "SKILL.md").write_text(
        "---\nname: grilling\n---\nИнтервью раундами по дереву решений.\n", encoding="utf-8")
    restore_home = set_home(home)
    try:
        m = RC.mentions('Добавь AC /grill-me, истории в @"Requirements/Истории пользователей.md", '
                        "поищи через @Tavily и /Users/кто-то/путь не навык", str(root), ["tavily"])
        names = [s[0] for s in m["skills"]]
        assert names == ["grill-me", "grilling"], f"навыки не найдены или без вложенного: {names}"
        assert m["mcp"] == ["tavily"], m["mcp"]
        assert m["files"] == ["Requirements/Истории пользователей.md"], m["files"]
        assert "Интервью раундами" in RC.skills_block(m["skills"])
        # без личного навыка grill-me ведёт на навык кита
        restore_home()
        restore_home = set_home(tmp / "пустой")
        path, body = RC.find_skill("grill-me", str(root), str(KIT))
        assert path.replace("\\", "/").endswith("aurora-grill/SKILL.md") and body, "grill-me не нашёл навык кита"
        # подсказки: / — навыки, @ — файлы, папки и серверы; секретов в подсказках нет
        assert any(i["value"] == "/aurora-grill" for i in RC.suggest(str(root), "/grill", [], str(KIT)))
        hints = RC.suggest(str(root), "@ист", ["tavily"], str(KIT))
        assert hints and hints[0]["value"] == "Requirements/Истории пользователей.md", hints
        assert not any(".env" in i["value"] for i in RC.suggest(str(root), "@env", [], str(KIT)))
        assert RC.suggest(str(root), "@tav", ["tavily"], str(KIT))[0]["kind"] == "mcp"
    finally:
        restore_home()


@test
def test_make_takes_attachments_skills_and_named_mcp_into_the_task(tmp: Path):
    """Производство артефакта кладёт в задание планировщика приложенное и названное.

    Файлы и вложения — в задание; метод навыка — туда же; MCP-сервер, названный в задаче или
    в поле `mcp` типа артефакта, — подключается сразу (`mcp_active`), остальные — по нужде.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp, git=False)
    (root / "Requirements").mkdir(exist_ok=True)
    (root / "Requirements" / "Истории.md").write_text("US-7: выгрузка в Excel\n", encoding="utf-8")
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"tavily": {"command": "npx"}}}),
                                   encoding="utf-8")
    att = importlib.import_module("request_context").save_attachment(
        str(root), "Постановка.txt", "Кнопка «Выгрузить» справа".encode())["path"]
    st = {"idea": "Сделай AC по @Requirements/Истории.md /aurora-grill, поищи через @tavily",
          "rounds": [], "context": [att]}
    here = os.getcwd()
    try:
        os.chdir(root)
        got = R.request_asks(str(root), st, {"mcp": "tavily, нет-такого"})
    finally:
        os.chdir(here)
    assert "US-7: выгрузка в Excel" in got["extra"], "файл по @ не в задании"
    assert "Кнопка «Выгрузить» справа" in got["extra"], "вложение не в задании"
    assert "/aurora-grill" not in got["extra"], "метод aurora-grill ушёл в задание второй раз"
    assert got["mcp"] == ["tavily"], f"названный сервер не подключается сразу: {got['mcp']}"
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    body = src[src.index("def run_make("):src.index("def report_make(")]
    assert "prompt_extra += req[\"extra\"]" in body and body.count("mcp_active=req[\"mcp\"]") == 2, \
        "задание планировщика и писателя без контекста или сервер не передан"
    assert '"--context"' in src, "у agent:make нет --context"


@test
def test_mcp_servers_start_only_when_needed(tmp: Path):
    """MCP по требованию: модель видит каталог и `mcp_connect`; сервер запускается при подключении.

    Раньше каждый вызов с инструментами поднимал все серверы роли и клал описания всех их
    инструментов в задание. Сервер, который не поднялся, не роняет прогон.
    """
    sys.path.insert(0, str(KIT / "scripts" / "agents"))
    import importlib
    AD = importlib.import_module("pydantic_ai_adapter")
    state = {"active": set()}
    assert "tavily — поиск" in AD.mcp_catalog({"tavily": {"about": "поиск"}})
    assert "подключён" in AD.mcp_activate(state, ["tavily"], "@Tavily") and state["active"] == {"tavily"}
    assert "уже подключён" in AD.mcp_activate(state, ["tavily"], "tavily")
    assert "нет" in AD.mcp_activate(state, ["tavily"], "brave")
    src = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    loop = src[src.index("def runtime("):]
    assert "lazy_mcp_toolsets(" in loop and 'task.get("mcp_active")' in loop, "адаптер поднимает все серверы"
    # Агент с серверами строится на одно задание: задания идут параллельно, и общий агент
    # отдал бы серверы, подключённые планировщиком, писателю соседнего вызова.
    assert AD.shared_agent_key({"url": "u", "model": "m", "tools": ["."],
                                "mcp": {"mcpServers": {"s": {}}}, "role": "planner"}) is None, \
        "агент с MCP-серверами общий для разных ролей и вызовов"

    vpy = Path.home() / ".aurora" / "venv" / "bin" / "python"
    if not vpy.exists():
        return          # venv с pydantic-ai не поставлен — живую проверку пропускаем
    server = tmp / "demo_server.py"
    server.write_text(textwrap.dedent("""
        import json, pathlib, sys
        pathlib.Path(sys.argv[1]).write_text("started")
        def send(o):
            sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\\n"); sys.stdout.flush()
        for line in sys.stdin:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            i, meth = m.get("id"), m.get("method")
            if meth == "initialize":
                send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": m["params"].get("protocolVersion"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "demo", "version": "1"}}})
            elif meth == "tools/list":
                send({"jsonrpc": "2.0", "id": i, "result": {"tools": [{"name": "echo", "description": "эхо",
                      "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}})
            elif meth == "tools/call":
                send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text",
                      "text": "эхо: " + (m["params"].get("arguments") or {}).get("text", "")}]}})
            elif i is not None:
                send({"jsonrpc": "2.0", "id": i, "result": {}})
    """), encoding="utf-8")
    scenario = tmp / "scenario.py"
    scenario.write_text(textwrap.dedent("""
        import json, os, sys
        sys.path.insert(0, sys.argv[1])
        import pydantic_ai_adapter as AD
        from pydantic_ai import Agent
        from pydantic_ai.models.function import FunctionModel
        from pydantic_ai.messages import ModelResponse, ToolCallPart, TextPart, ToolReturnPart
        S, mark = sys.argv[2], os.path.join(sys.argv[2], "started.txt")
        cfg = {"mcpServers": {"demo": {"command": sys.executable, "args": [os.path.join(S, "demo_server.py"), mark]},
                              "broken": {"command": "/nonexistent/mcp-nope"}}}
        state = {"active": set()}
        ts = AD.lazy_mcp_toolsets(cfg, {}, S, "", state)
        seen = []
        def fn(messages, info):
            seen.append([sorted(t.name for t in info.function_tools), os.path.exists(mark)])
            calls = {1: ("mcp_connect", {"name": "demo"}), 2: ("demo_echo", {"text": "привет"}),
                     3: ("mcp_connect", {"name": "broken"}), 4: ("mcp_connect", {"name": "broken"})}
            if len(seen) in calls:
                return ModelResponse(parts=[ToolCallPart(*calls[len(seen)])])
            rets = [str(p.content) for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
            return ModelResponse(parts=[TextPart(json.dumps(rets, ensure_ascii=False))])
        out = Agent(FunctionModel(fn), toolsets=ts).run_sync("go").output
        print(json.dumps({"seen": seen, "returns": json.loads(out)}, ensure_ascii=False))
    """), encoding="utf-8")
    cp = subprocess.run([str(vpy), str(scenario), str(KIT / "scripts" / "agents"), str(tmp)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240)
    assert cp.returncode == 0, cp.stderr[-1500:]
    d = json.loads(cp.stdout.strip().splitlines()[-1])
    assert d["seen"][0] == [["mcp_connect"], False], f"сервер поднят до нужды: {d['seen'][0]}"
    # Инструменты сервера — с приставкой его имени: `demo` → `demo_echo` (1.143.1).
    assert "demo_echo" in d["seen"][1][0] and d["seen"][1][1], f"подключённый сервер не дал инструментов: {d['seen'][1]}"
    assert "эхо: привет" in d["returns"], d["returns"]
    assert any("не запустился" in r for r in d["returns"]), f"сломанный сервер без объяснения: {d['returns']}"


@test
def test_productivity_takes_mentions_and_attachments(tmp: Path):
    """«Продуктивность»: подсказки по @ и /, приложить файл, плашки, --context движку."""
    view = (KIT / "cockpit/modules/work/view.js").read_text(encoding="utf-8")
    html = (KIT / "cockpit/modules/work/view.html").read_text(encoding="utf-8")
    for token in ('id="makeAttach"', 'id="makeFile"', 'id="makeRefs"', 'id="makeSuggest"'):
        assert token in html, f"нет части поля задачи: {token}"
    assert '"/api/context/upload"' in view and '"/api/context/suggest?project="' in view
    assert '["--context", r.path]' in view, "ссылки и вложения не уходят движку"
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'u.path == "/api/context/upload"' in src and 'u.path == "/api/context/suggest"' in src
    import importlib
    sys.path.insert(0, str(KIT / "scripts"))
    IA = importlib.import_module("install_aurora")
    assert ".opencode/context/" in IA.GITIGNORE_BLOCK, "вложения уедут в git проекта"
    assert "scripts/request_context.py" in (KIT / "engine_manifest.txt").read_text(encoding="utf-8"), \
        "модуль контекста не доедет до проектов"


@test
def test_a_link_to_a_section_opens_it_without_waiting_for_health(tmp: Path):
    """Ссылка на раздел (`#work|ПРОЕКТ`) открывает его сразу, а не после здоровья проекта.

    Найдено 24.09.2026: старт панели ждал `pick` — а тот ждёт `/api/health`, на крупной базе
    десятки секунд, — и только потом открывал раздел из адреса. Всё это время человек
    смотрел на Мостик и решал, что ссылка не сработала.
    """
    ui = ui_source()
    boot = ui[ui.index("async function boot("):ui.index("/* ---------------- мостик ---------------- */")]
    tail = boot[boot.index('location.hash.slice(1)'):]
    assert "const picking = p ? pick(p, false) : null;" in tail, "проект выбирается с ожиданием здоровья"
    assert tail.index("if (v) show(v);") < tail.index("if (picking) await picking;"), \
        "раздел из адреса открывается только после здоровья"
    pick = ui[ui.index("async function pick("):]
    assert pick.index("S.project = p;") < pick.index('await api("/api/health'), \
        "pick ставит проект только после здоровья — раздел откроется без проекта"


@test
def test_compare_benchmarks_variants_offline(tmp: Path):
    """Бенчмарк вариантов ретрива гоняется без сети и ничего не записывает.

    Векторного шлюза в фикстуре нет — близость по смыслу честно пуста, и стенд мерит
    словесную половину гибрида, а не отказывает. Без этого теста сравнение вариантов
    проверялось бы только руками на живой базе — ровно то, чего бенчмарк заводился
    не допустить.
    """
    root = make_project(tmp)
    for i in range(4):
        card(root, f"Concepts/Приём-номер-{i}.md",
             f"Приём номер {i} — это способ вести учёт заявок в контуре проекта. "
             f"Тезис карточки написан своими словами и достаточно длинный для замера.",
             status="knowledge", kind="knowledge", distilled="2026-01-01")

    cp = run("kb_search_quality.py", "--compare", "base,related", "--sample", "0",
             cwd=root, expect_rc=0)
    assert "| base |" in cp.stdout and "| related |" in cp.stdout, \
        f"таблица сравнения не собралась:\n{cp.stdout[:600]}"
    assert "Бенчмарк вариантов" in cp.stdout
    hist = root / "AuroraKnowledgeDB/meta/search-quality.json"
    assert not hist.exists(), "бенчмарк пишет в историю замеров — это стенд, не прогон"

    bad = run("kb_search_quality.py", "--compare", "base,nosuchkey=1", cwd=root)
    assert bad.returncode != 0 or "неизвестн" in (bad.stdout + bad.stderr), \
        "опечатка в имени ключа прошла молча — варианты сравнивались не те"


@test
def test_retrieval_flag_rejects_unknown_keys(tmp: Path):
    """--retrieval с опечаткой в ключе — отказ, а не молчаливый пропуск.

    Переключатель, который не сработал, выглядит как применившийся: замер сравнивает
    не те варианты, и вывод бенчмарка врёт. Поэтому неизвестный ключ — ошибка запуска.
    """
    root = make_project(tmp)
    card(root, "Concepts/Любая.md", "Любое знание.", status="knowledge", kind="knowledge")
    cp = run("ctx_pack.py", "любая", "--retrieval", "hop_relaited=1", cwd=root)
    assert cp.returncode == 1, "опечатка в ключе --retrieval не отклонена"
    assert "hop_relaited" in cp.stderr, "отказ не назвал неизвестный ключ"


@test
def test_review_checklist_lives_in_the_template(tmp: Path):
    """Чек-лист, веса, словарь и правила для модели читаются из шаблона — одна копия.

    Две копии вопросов (в шаблоне для человека и в коде для скрипта) разошлись бы на
    первой правке: человек читает одно, а оценку считают по другому.
    """
    rr = _review_module()
    cl = _review_checklist(rr)
    assert cl["version"] == "2.0", cl["version"]
    assert len(cl["profiles"]["us"]) >= 20 and len(cl["profiles"]["alg"]) >= 20
    for prof in cl["profiles"].values():
        ids = [c["id"] for c in prof]
        assert len(ids) == len(set(ids)), "идентификаторы вопросов повторяются"
        for c in prof:
            assert c["severity"] in rr.WEIGHT and c["question"] and c["fix"], c
            assert c["decider"] in ("модель", "код", "код+модель"), c
            assert not c["when"].startswith("то же условие"), \
                "ссылка на условие не раскрыта — модель не поймёт, о чём речь"
    assert {"Оценочные без критерия", "Рабочие пометки"} <= set(cl["lexicon"]), cl["lexicon"]
    assert "Одинаковый текст" in cl["rules"] and "JSON" in cl["rules"], "правила не прочитаны"


@test
def test_review_score_is_computed_by_code(tmp: Path):
    """Оценку и вердикт считает код по ответам; «нет» без доказательства не принимается."""
    rr = _review_module()
    cl = _review_checklist(rr)
    ids = [c["id"] for c in cl["profiles"]["us"] if c["decider"] != "код"]
    got = rr.parse_answer(_answer(cl, "us", {"US-09": {"v": "no", "evidence": ""}}), ids)
    assert got["checks"]["US-09"]["v"] == "unknown", "«нет» без цитаты принят"
    fenced = "```json\n" + _answer(cl, "us", {"US-09": {"v": "нет", "evidence": "шаг 3"}}) + "\n```"
    assert rr.parse_answer(fenced, ids)["checks"]["US-09"]["v"] == "no", "ответ в ограде не разобран"
    assert rr.parse_answer("не JSON вовсе", ids) is None

    l0 = rr.layer0("Чистый текст без пометок.", cl["lexicon"], [])
    ok = {"ok": True, **rr.parse_answer(_answer(cl, "us"), ids)}
    ans = rr.vote(cl, "us", [ok, ok, ok], l0)
    res = rr.score(cl, "us", ans, False, True, 3)
    assert res["score"] == 10.0 and res["verdict"] == "готова к передаче", res

    # одно мелкое «нет»: 10 × 52/53 = 9,81 → округление вниз 9,8
    minor = {"ok": True, **rr.parse_answer(
        _answer(cl, "us", {"US-09": {"v": "no", "evidence": "шаг 3: таблица t_doc"}}), ids)}
    res = rr.score(cl, "us", rr.vote(cl, "us", [minor] * 3, l0), False, True, 3)
    assert res["score"] == 9.8 and res["verdict"] == "готова к передаче", res

    # важное «нет» закрывает ворота при любом итоге
    major = {"ok": True, **rr.parse_answer(
        _answer(cl, "us", {"US-10": {"v": "no", "evidence": "AC п.2"}}), ids)}
    res = rr.score(cl, "us", rr.vote(cl, "us", [major] * 3, l0), False, True, 3)
    assert res["verdict"] == "доработать" and res["score"] >= 9.0, res

    # «н/п» на вопрос «всегда» недопустим — становится «не определено» и держит ворота
    na = {"ok": True, **rr.parse_answer(_answer(cl, "us", {"US-12": {"v": "na"}}), ids)}
    ans = rr.vote(cl, "us", [na] * 3, l0)
    assert ans["US-12"]["v"] == "unknown", ans["US-12"]
    assert rr.score(cl, "us", ans, False, True, 3)["verdict"] == "доработать"

    # голосование: 2 из 3 — принято, но вопрос неустойчив
    ans = rr.vote(cl, "us", [ok, ok, major], l0)
    assert ans["US-10"]["v"] == "yes" and not ans["US-10"]["stable"], ans["US-10"]
    assert rr.needs_more(cl, "us", ans, rr.score(cl, "us", ans, False, True, 3)), \
        "неустойчивый важный вопрос не вызвал дополнительных прогонов"

    # голосование лагерями: «да» и «н/п» — один лагерь, воздержание не перевешивает двух
    # согласных. Первый боевой прогон дал ничьи 2:2 на восьми вопросах из-за буквального счёта.
    def run_with(v, ev=""):
        return {"ok": True, **rr.parse_answer(_answer(cl, "us", {"US-20": {"v": v, "evidence": ev}}), ids)}
    ans = rr.vote(cl, "us", [run_with("yes"), run_with("na"), run_with("na")], l0)
    assert ans["US-20"]["v"] == "na" and ans["US-20"]["stable"], ans["US-20"]
    ans = rr.vote(cl, "us", [run_with("yes"), run_with("yes"), run_with("unknown"),
                             run_with("unknown")], l0)
    assert ans["US-20"]["v"] == "yes" and not ans["US-20"]["stable"], ans["US-20"]
    ans = rr.vote(cl, "us", [run_with("yes"), run_with("no", "п.3"), run_with("unknown")], l0)
    assert ans["US-20"]["v"] == "unknown" and ans["US-20"]["tie"], "ничья не распознана"

    # рабочие пометки решает код, а не модель
    l0 = rr.layer0("Шаг 2 ??? уточнить", cl["lexicon"], [])
    ans = rr.vote(cl, "us", [ok] * 3, l0)
    assert ans["US-23"]["v"] == "no" and ans["US-23"]["votes"] == "код", ans["US-23"]

    # неполное чтение обязательной страницы не даёт пройти
    res = rr.score(cl, "us", rr.vote(cl, "us", [ok] * 3, rr.layer0("", cl["lexicon"], [])),
                   True, True, 3)
    assert res["verdict"] == "доработать" and "обязательная" in " ".join(res["reasons"])


@test
def test_review_page_runs_without_a_human(tmp: Path):
    """Страница → прочтение связей → прогоны → голосование → отчёт, без единого вопроса.

    Ссылка на ещё не созданную страницу — предусловие «сделать», не дефект и не пробел
    в чтении. Закрытая по правам обязательная страница — пробел: ворота закрыты.
    """
    rr = _review_module()
    cl = _review_checklist(rr)
    view = ('<h2>Предусловия US</h2><a href="/pages/viewpage.action?pageId=20">Форма</a>'
            '<a class="createlink" href="/pages/createpage.action?spaceKey=S&amp;title=ALG-9">ALG-9</a>'
            '<h2>Описание</h2><p>Как оператор</p>')
    # ссылки на версии самой страницы («История изменений»), профили и личные пространства —
    # не артефакты: первый боевой прогон принял четыре такие ссылки за удалённые страницы
    noise = ('<a class="view-historical-version-trigger" href="/pages/viewpage.action?pageId=99">v. 20</a>'
             '<a href="/display/~ivanov">Иванов</a>'
             '<a href="/users/viewuserprofile.action?username=x">x</a>')
    assert rr.links_with_sections(noise, "https://wiki") == [], "служебные ссылки приняты за артефакты"
    main = {"id": "10", "status": "ok", "title": "US-1.2.3. Приём заявки", "url": "u", "version": 4,
            "text": "Как оператор я хочу...", "links": rr.links_with_sections(view, "https://wiki"),
            "author": "А. Автор", "modified": "2025-05-02", "created": "2025-01-10"}
    form = {"id": "20", "status": "ok", "title": "Экранная форма", "text": "поле ИНН обязательно",
            "links": [], "version": 2}
    calls = []

    def fake(cfg_, role, messages, **kw):
        calls.append(role)
        msgs = stub_messages(messages, kw)
        assert "Экранная форма" in msgs[1]["content"], "связанная страница не попала в промпт"
        assert "ещё не создана" in msgs[1]["content"], "будущая страница не объяснена модели"
        return {"ok": True, "text": _answer(cl, "us"), "model": "m", "seen": 1, "cut": 0}

    cfg = {"request_timeout": 5}
    rec = rr.review_page(cfg, cl, _FakeReader({"10": main, "20": form}), "10", runs=3,
                         call=fake)
    assert len(calls) == 3, f"прогонов {len(calls)}, ждали 3 (без эскалации)"
    assert rec["verdict"] == "готова к передаче" and rec["score"] == 10.0, rec
    assert not rec["incomplete"], "будущая страница посчитана пробелом в чтении"
    assert rec["code"] == "US-1.2.3" and rec["profile"] == "us"
    report = rr.render(rec, cl)
    assert "| Версия шаблона ревью | 2.0 (`review_v2.0.md`) |" in report, "версия шаблона не в итоге"
    assert report.startswith("---\ntype: review"), "у отчёта не одна шапка"

    # обязательная форма закрыта правами → неполное ревью, ворота закрыты
    locked = {"10": main, "20": {"id": "20", "status": "forbidden"}}
    rec = rr.review_page(cfg, cl, _FakeReader(locked), "10", runs=3,
                         call=lambda *a, **k: {"ok": True, "text": _answer(cl, "us"),
                                               "model": "m", "seen": 1, "cut": 0})
    assert rec["incomplete"] and rec["verdict"] == "доработать", rec["verdict"]


@test
def test_review_batch_resumes_and_summarizes(tmp: Path):
    """Сводка пакета пересчитывается из журнала целиком и переживает обрыв."""
    rr = _review_module()
    bdir = tmp / "batch_x"
    bdir.mkdir()
    base = {"status": "ok", "profile": "us", "template_version": "2.0", "as_of": "",
            "coverage": 1.0, "unknown": 0, "unstable": 0, "runs": 3, "good_runs": 3,
            "failed": {"critical": 0, "major": 0, "minor": 0}}
    rows = [dict(base, page_id="1", code="US-1", score=9.8, verdict="готова к передаче",
                 modified="2025-02-01", author="А",
                 answers={"US-09": {"v": "no", "stable": True}}),
            dict(base, page_id="2", code="US-2", score=6.0, verdict="доработать",
                 modified="2025-08-01", author="Б",
                 answers={"US-09": {"v": "no", "stable": False}, "US-10": {"v": "yes", "stable": True}}),
            {"page_id": "3", "status": "forbidden", "verdict": "не оценено", "reasons": ["страница: forbidden"]}]
    (bdir / "results.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                                        encoding="utf-8")
    keys = rr.done_keys(bdir / "results.jsonl")
    assert set(keys) == {"1", "2"}, "непрочитанную страницу не повторят при продолжении"
    md = rr.summarize(bdir)
    assert "Прошли порог | 1 из 2" in md, md
    assert "2025-Q1" in md and "2025-Q3" in md, "нет разбивки по кварталам"
    assert "| US-09 | 2 | 100% |" in md, "частые дефекты посчитаны неверно"
    assert "Устойчивость чек-листа" in md and "| US-09 | 1 | 50% |" in md, md
    assert "По авторам" not in md, "авторская разбивка должна включаться явно"
    assert (bdir / "summary.csv").is_file()


@test
def test_review_engine_is_wired_into_the_kit(tmp: Path):
    """Команда в реестре, скрипт в манифесте, шаблон в поставке."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "make:review-auto" in reg and "review_run.py" in reg, "команды нет в реестре"
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "scripts/review_run.py" in man, "движок ревью не едет в проекты"
    assert (KIT / "scaffold/TemplatesCommon/review_v2.0.md").is_file()

@test
def test_cockpit_modules_are_folders(tmp: Path):
    """Раздел панели — папка: манифест, разметка, скрипт, строки. Без правки сервера.

    Тем же приёмом подключается скин. Проверяем, что реестр собирается, путь наружу
    папки модуля не принимается и обязательные файлы на месте: раздел без разметки или
    без скрипта поднимется пустым экраном, и человек решит, что сломалась панель.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    mods = ck.modules()
    assert mods, "ни одного модуля — переезд разделов в папки откатился?"
    for m in mods:
        assert not m.get("error"), f"модуль {m['id']}: {m.get('error')}"
        assert m["group"] in ck.MODULE_GROUPS, f"модуль {m['id']}: чужая группа меню"
        assert m["name"].get("ru"), f"модуль {m['id']} без русского имени"
        assert not m["behind"], \
            f"модуль {m['id']} собран под {m['for']}, а ядро {ck.kit_version()}"
        for need in m["needs"]:
            assert need in ("project", "kit"), f"модуль {m['id']}: непонятное needs={need}"
        for f in ("view.html", "view.js", "module.json"):
            assert ck.module_file(m["id"], f), f"модуль {m['id']}: нет {f}"
        # Команды, которые раздел зовёт, обязаны быть в реестре: кнопка, ведущая в
        # никуда, — тот же дефект, что шаг сценария с несуществующей командой.
        known = {r["cmd"] for r in ck.registry()}
        for cmd in m["commands"]:
            assert cmd in known, f"модуль {m['id']} зовёт несуществующую команду {cmd}"

    # Раздел грузится модулем ES и тянет соседние файлы относительным импортом — тот
    # уходит без токена. Значит файлы раздела обязаны отдаваться без него, иначе раздел,
    # разделённый на файлы, молча не поднимется.
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    guard = src[src.index("def guarded("):src.index("def send_json(")]
    assert 'self.path.startswith("/modules/")' in guard, \
        "файлы раздела требуют токена — относительный импорт внутри раздела не пройдёт"

    # Путь наружу папки модуля и чужие расширения не принимаются.
    assert ck.module_file("reports", "../../VERSION") == "", "модуль читается вне своей папки"
    assert ck.module_file("../skins", "zine.css") == "", "имя модуля с путём наружу"
    assert ck.module_file("reports", "module.json.bak") == "", "отдан файл неизвестного типа"

    # Порядок меню детерминирован: группы по списку, внутри — по order.
    order = [(ck.MODULE_GROUPS.index(m["group"]), m["order"], m["id"]) for m in mods]
    assert order == sorted(order), "реестр модулей отдан не в порядке меню"


@test
def test_cockpit_module_strings_live_in_catalogues(tmp: Path):
    """Ни одной русской строки в коде раздела: надписи живут в каталогах.

    Проверка ровно та, ради которой каталоги и заводились: строка, забытая в коде,
    переводится только правкой кода, и перевод раздела выглядит сделанным, пока кто-то
    не откроет его на другом языке.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    for m in ck.modules():
        mid = m["id"]
        ru = json.loads(Path(ck.module_file(mid, "i18n/ru.json")).read_text(encoding="utf-8"))
        keys = {k for k in ru if not k.startswith("_")}
        assert keys, f"модуль {mid}: пустой каталог строк"
        for k in keys:
            assert k.startswith(mid + "."), \
                f"модуль {mid}: ключ {k} не своего имени — разделы начнут спорить за ключи"

        # Читаем все файлы раздела: у близнецов бывает общий кусок разметки экрана.
        folder = Path(ck.module_file(mid, "view.js")).parent
        js = "\n".join(f.read_text(encoding="utf-8") for f in sorted(folder.glob("*.js")))
        html = Path(ck.module_file(mid, "view.html")).read_text(encoding="utf-8")
        used = set(re.findall(r'\bt\("([a-z][\w.]+)"', js))
        used |= set(re.findall(r'data-i18n(?:-ph|-title|-aria|-html)?="([^"]+)"', html))
        # Ключ бывает собран из куска (`t("commands.ns." + ns)`) или лежит в таблице
        # раздела: начало ключа покрывает весь набор, а литерал — сам себя.
        used |= set(re.findall(r'"(%s\.[\w.]+)"' % re.escape(mid), js))
        # Ключ ядра разделу доступен: общие надписи переводятся один раз и живут там.
        core = {k for k in json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
                if not k.startswith("_")}
        # Подсказка кнопки (`data-help`) — приставка трёх ключей: что делает, пример и
        # результат. Считается найденной, только если в каталоге есть все три.
        helps = set(re.findall(r'data-help="([^"]+)"', html)) | set(
            re.findall(r'"data-help":\s*"([^"]+)"', js))
        missing = sorted({h for h in helps for part in ("what", "how", "result")
                          if f"{h}.{part}" not in keys and f"{h}.{part}" not in core})
        used -= helps
        missing += sorted(u for u in used
                          if not u.endswith(".") and u not in keys and u not in core)
        assert not missing, f"модуль {mid}: спрашивает ключи, которых нет в каталоге: {missing}"

        # Русский текст в строковых литералах кода — то, что переезд и убирает.
        # Комментарии остаются русскими: это объяснение для того, кто правит код.
        # Отдельный случай — ключи данных движка («битые ссылки» — имя вида ошибки из
        # lint, а не надпись). Их не переводят, но и спутать с забытой надписью нельзя:
        # такая строка помечается в коде словами «данные движка».
        body = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
        body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
        forgotten = []
        for line in body.splitlines():
            if "данные движка" in line:
                continue
            for lit in re.findall(r'"[^"\n]*"|\'[^\'\n]*\'', line.split("//")[0]):
                if re.search("[а-яА-ЯёЁ]", lit):
                    forgotten.append(lit)
        assert not forgotten, \
            f"модуль {mid}: русский текст остался в коде, а не в каталоге: {forgotten[:3]}"

        # Перевод полон: иначе английский экран наполовину русский, и это не видно,
        # пока не откроешь.
        en_path = ck.module_file(mid, "i18n/en.json")
        assert en_path, f"модуль {mid}: нет английского каталога"
        en = json.loads(Path(en_path).read_text(encoding="utf-8"))
        lack = sorted(k for k in keys if not str(en.get(k, "")).strip())
        assert not lack, f"модуль {mid}: без перевода остались {lack[:5]}"


@test
def test_cockpit_core_strings_live_in_catalogues(tmp: Path):
    """Ядро панели переводится целиком: ни одной надписи в коде и разметке.

    Разделы эту проверку уже проходят, а ядро — Мостик, консоль, маршруты, настройки,
    карточка агента — держало текст прямо в коде. Английский язык от этого выглядел
    сделанным, пока человек не открывал экран: меню и заголовки переводились, а всё,
    ради чего в раздел заходят, оставалось русским. Живая жалоба 24.09.2026.

    Исключений ровно два, и оба видны глазами:
      • слова самого движка (`"битые ссылки"` — имя вида находки, а не надпись) —
        такая строка помечается в коде словами «данные движка»;
      • сообщения в консоль браузера — их читает тот, кто правит код, а не человек.
    """
    src = ui_source()

    # ── 1. каталоги ядра сходятся между собой ──────────────────────────────
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    keys = {k for k in ru if not k.startswith("_")}
    lack = sorted(k for k in keys if not str(en.get(k, "")).strip())
    assert not lack, f"ядро: без английского остались {lack[:5]} (всего {len(lack)})"

    # ── 2. код ядра ────────────────────────────────────────────────────────
    body = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", src, re.S))
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)       # пояснения остаются русскими
    forgotten = []
    for line in body.splitlines():
        if "данные движка" in line or re.search(r"\bconsole\.\w+\(", line):
            continue
        code = line.split("//")[0]
        if code.strip().startswith("//"):
            continue
        for lit in re.findall(r'"[^"\n]*"|\'[^\'\n]*\'|`[^`\n]*`', code):
            if re.search("[а-яА-ЯёЁ]", lit):
                forgotten.append(lit.strip())
    assert not forgotten, \
        f"надписи остались в коде ядра ({len(forgotten)}): {forgotten[:4]}"

    # ── 3. разметка страницы ───────────────────────────────────────────────
    markup = src[src.index("<body"):src.index("<script>")]
    markup = re.sub(r"<!--.*?-->", "", markup, flags=re.S)
    # Абзац с разметкой внутри переводится целиком (`data-i18n-html`): цветные слова
    # и моноширинные имена файлов внутри него — часть той же надписи.
    while True:
        m = re.search(r"<(\w+)[^>]*data-i18n-html[^>]*>", markup)
        if not m:
            break
        close = markup.index(f"</{m.group(1)}>", m.end())
        markup = markup[:m.start()] + markup[close:]

    ru_text = re.compile("[а-яА-ЯёЁ]")
    naked = []
    for m in re.finditer(r"<([a-z][\w-]*)((?:[^<>\"]|\"[^\"]*\")*)>", markup, re.S):
        attrs = m.group(2)
        for attr, key in (("title", "data-i18n-title"), ("placeholder", "data-i18n-ph"),
                          ("aria-label", "data-i18n-aria")):
            v = re.search(r'(?<![\w-])' + attr + r'="([^"]*)"', attrs)
            if v and ru_text.search(v.group(1)) and key not in attrs:
                naked.append(f"<{m.group(1)} {attr}=…{v.group(1)[:30]}>")
        tail = markup[m.end():]
        text = tail[:tail.index("<")] if "<" in tail else tail
        if ru_text.search(text) and "data-i18n" not in attrs:
            naked.append(f"<{m.group(1)}>{text.strip()[:40]}")
    assert not naked, f"надписи остались в разметке ({len(naked)}): {naked[:4]}"

    # ── 4. язык не забыт в помощниках ──────────────────────────────────────
    # Дата и время собираются локалью языка, а не всегда русской: «14 сент.» на
    # английском экране выглядит как недоделка, потому что ею и является.
    left = body.replace('S.lang === "en" ? "en-GB" : "ru-RU"', "").replace(
        'const loc = S.lang === "en" ? "en-GB" : "ru-RU"', "")
    assert '"ru-RU"' not in left, "где-то осталась жёсткая русская локаль времени"


@test
def test_cockpit_serves_module_catalogues_together(tmp: Path):
    """Панель получает строки ядра и модулей одним каталогом, ключ ядра — главнее."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    ru = ck.i18n_catalogue("ru")["strings"]
    core = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    for k in core:
        if not k.startswith("_"):
            assert ru[k] == core[k], f"ключ ядра {k} перебит модулем"
    for m in ck.modules():
        mod_ru = json.loads(Path(ck.module_file(m["id"], "i18n/ru.json")).read_text(encoding="utf-8"))
        for k, v in mod_ru.items():
            if not k.startswith("_"):
                assert ru.get(k) == v, f"строка модуля {m['id']} не доехала до панели: {k}"

    en = ck.i18n_catalogue("en")
    assert en["lang"] == "en", "английский каталог не собрался"
    assert en["strings"].get("files.title") != core["files.title"], \
        "английский каталог отдал русские строки"


@test
def test_cockpit_core_mounts_modules_and_keeps_menu(tmp: Path):
    """Ядро умеет поднимать раздел из папки, а меню собирается по группам и порядку."""
    # Здесь нужен именно монолит: проверяем, что переехавший раздел из него ушёл.
    ui = ui_source()  # монолит панели читаем сознательно

    for needed in ("async function loadModules", "async function mountModule",
                   "function moduleCtx", "/api/modules", "navgroup"):
        assert needed in ui, f"в панели нет «{needed}» — модули не поднимутся"
    assert "if (MODULES.has(view)) mountModule(view" in ui, \
        "маршрутизация не знает про модули"
    # Каждая кнопка меню объявляет порядок: модуль встаёт между своими, а не в конец.
    buttons = re.findall(r'<button data-view="([a-z]+)"([^>]*)>', ui)
    for view, rest in buttons:
        assert 'data-order="' in rest, f"кнопка «{view}» без data-order — модулю некуда встать"
    # Разделы, переехавшие в папки, из монолита убраны целиком.
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    for m in ck.modules():
        assert f'id="view-{m["id"]}"' not in ui, \
            f"раздел {m['id']} остался и в монолите, и в папке — путь раздвоился"


@test
def test_cockpit_scripts_parse(tmp: Path):
    """Скрипты панели и разделов разбираются без ошибок.

    Опечатка в разделе не видна ни одному другому тесту: панель откроется, а раздел
    молча не поднимется — человек решит, что его убрали. node на машине может не быть
    (кит обходится стандартной библиотекой Python), тогда проверка пропускается.
    """
    node = shutil.which("node")
    if not node:
        return

    def check(name: str, code: str, module: bool):
        f = tmp / (name + (".mjs" if module else ".js"))
        f.write_text(code, encoding="utf-8")
        cp = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert cp.returncode == 0, f"{name} не разбирается:\n{cp.stderr.strip()[:800]}"

    ui = ui_source()
    # Ядро — один инлайновый скрипт; токен и каталог строк сервер подставляет при выдаче,
    # поэтому для разбора ставим на их место заглушки.
    body = ui.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    body = body.replace("__AURORA_TOKEN__", "x").replace('"__AURORA_I18N__"', "{}")
    check("core", body, module=False)

    mods = KIT / "cockpit" / "modules"
    for view in sorted(mods.glob("*/view.js")):
        check(view.parent.name, view.read_text(encoding="utf-8"), module=True)


@test
def test_fields_are_readable_in_both_themes(tmp: Path):
    """Поле ввода и выпадающий список читаются в обеих темах.

    Живая жалоба 24.09.2026: в тёмной теме поля с выпадающим меню светлые, а текст в них
    светлый — не видно ничего. Причина в том, что цвет текста наследуется от панели, а фон
    поля рисует браузер по своему усмотрению. Правило должно быть общим, а не по классу:
    половина списков в панели заведена без класса, и «покрасили те, что помним» — это ровно
    то, как дефект и появился.
    """
    ui = ui_source()
    css = ui[ui.index("<style>"):ui.index("</style>")]

    assert 'color-scheme:dark' in css and 'color-scheme:light' in css, \
        "браузеру не сказано, какие рисовать системные части: раскрытый список останется светлым"

    rule = [l for l in css.splitlines() if l.startswith("input:not([type=checkbox])")]
    assert rule, "нет общего правила для полей — красить их по классу значит забыть половину"
    block = css[css.index(rule[0]):]
    block = block[:block.index("}") + 1]
    for need in ("background:var(--field-bg)", "color:var(--text)"):
        assert need in block, f"поле без {need}: в одной из тем оно станет нечитаемым"
    assert "option{background:var(--surface-1);color:var(--text)}" in css, \
        "строки раскрытого списка не покрашены"

    # Токены поля обязаны быть у каждого скина: иначе «покрасили» значит «в одном скине».
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    base = css[css.index("--field-bg"):]
    assert "--field-bg:var(--surface-1)" in base, "у поля нет значения по умолчанию"
    for skin in ck.skins():
        text = ck.skin_css(skin["id"])
        if "--field-bg" in text:
            assert "--field-border" in text, \
                f"скин {skin['id']} задал фон поля, но не его рамку"


@test
def test_page_template_is_hidden_from_the_model_but_quotes_stay_verbatim(tmp: Path):
    """Шаблон страницы модель не видит, а дословный раздел карточки остаётся дословным.

    PRJ-C 30.09.2026: «При правках стори… писать комментарий» стоит на 427 страницах. Превью
    секции (900 знаков) у алгоритма целиком было шаблоном, поиск кандидатов находил по нему
    карточку с тезисом о том же шаблоне, и знание 59 страниц легло в одну карточку. С
    1.145.0 абзац, дословно стоящий в двадцати файлах, — шаблон: из раскадровки он пропадает
    (кроме канонического источника — первого по пути), строка таблицы истории — тоже, шапка
    таблицы остаётся. Переносится в карточку текст по-прежнему целиком.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    root = make_project(tmp)
    paths = _template_mirror(root)
    blocks = A.template_blocks(str(root))
    assert TEMPLATE_PAR in blocks and TEMPLATE_ROW in blocks, sorted(blocks)[:5]
    assert all("Шаг первый" not in p for p in blocks), "своё знание страницы принято за шаблон"
    assert (root / ".opencode/cache/template_blocks.json").is_file(), "словарь не закэширован"

    rel = paths[5].relative_to(root).as_posix()
    cp = run("build_plan.py", "--slice", rel, "--slice-chars", "900", cwd=root)
    head = cp.stdout.split("ЗАДАНИЕ АССИСТЕНТУ")[0]
    assert "ОБЯЗАТЕЛЬНО писать комментарий" not in head, f"шаблон в превью:\n{head}"
    assert "@Иванова" not in head, f"строка истории в превью:\n{head}"
    assert "| Код | RYk:ALG-105 |" in head, "шапку таблицы спрятали вместе со строкой"
    assert "считает сумму налога" in head and "Шаг первый" in head, head

    canon = paths[0].relative_to(root).as_posix()
    assert blocks[TEMPLATE_PAR] == canon, blocks[TEMPLATE_PAR]
    head0 = run("build_plan.py", "--slice", canon, "--slice-chars", "900", cwd=root).stdout
    assert "ОБЯЗАТЕЛЬНО писать комментарий" in head0, "канонический источник потерял абзац"

    run("build_plan.py", "--card", "Алгоритм пять", "--source", rel, "--sections", "1",
        "--to", "Processes", "--apply", cwd=root, expect_rc=0)
    made = (root / "AuroraKnowledgeDB/Processes/Алгоритм-пять.md").read_text(encoding="utf-8")
    assert TEMPLATE_PAR in made and TEMPLATE_ROW in made, \
        "раздел «перенесено дословно» перестал быть дословным"

    # секция из одного шаблона помечена для модели служебной
    only = root / "Sources/Confluence/Алгоритмы/Пустая_форма.md"
    only.write_text(f"---\npage_id: 9999\n---\n# Пустая форма\n\n# Описание\n\n{TEMPLATE_PAR}\n\n"
                    f"{TEMPLATE_PAR2}\n\n# Шаги\n\nЕдинственный шаг — отправить квитанцию в "
                    "налоговый орган по телекоммуникационному каналу связи в течение суток, "
                    "а при отказе канала — повторить отправку через час и записать попытку в "
                    "журнал обмена с указанием времени и кода ошибки шлюза.\n", encoding="utf-8")
    slice_ = run("build_plan.py", "--slice", only.relative_to(root).as_posix(), cwd=root).stdout
    assert A.TEMPLATE_ONLY in slice_, slice_
    R = importlib.import_module("agent_runner")
    rows = R.SECTION_RE.findall(slice_.split("ЗАДАНИЕ АССИСТЕНТУ")[0])
    assert any(prev.startswith(A.TEMPLATE_ONLY) for _n, _t, _s, prev in rows), rows


@test
def test_a_card_born_of_the_page_template_is_archived_and_its_sources_replanned(tmp: Path):
    """Карточка, собранная по шаблону, — в архив; чужие страницы — обратно в план.

    Два вида вреда от шаблона в уже собранной базе PRJ-C. «Предусловия-пользовательских-
    историй» — карточка, чей тезис пересказывает инструкцию авторам, к которой 38 историй
    пришли одним абзацем: она уходит в архив, а источники — в план. «Сохранение-текста-
    комментария» — законная карточка алгоритма, к которой шаблон притянул 54 чужих: блок,
    пришедший абзацем шаблона и не называющий её сущность, отвязывается. Ссылки на
    ушедшую карточку становятся текстом, строка из одной ссылки уходит.
    """
    import importlib, json
    sys.path.insert(0, str(SCRIPTS))
    root = make_project(tmp)
    paths = [p.relative_to(root).as_posix() for p in _template_mirror(root)]
    q = lambda srcs: "".join(f"### {s}\n\n# Описание\n\n{TEMPLATE_PAR}\n\nАлгоритм {i}.\n\n"
                             for i, s in enumerate(srcs))
    srcs_block = lambda srcs: "sources:\n" + "".join(f'  - "{s}"\n' for s in srcs)
    kb = root / "AuroraKnowledgeDB"
    (kb / "Processes").mkdir(parents=True, exist_ok=True)
    (kb / "Processes/Комментарий-при-правке-стори.md").write_text(
        f'---\ntitle: "Комментарий при правке стори"\nstatus: draft\ntype: process\n'
        f'kind: knowledge\ndistilled: 2026-09-20\n{srcs_block(paths[3:8])}---\n\n'
        "Комментарий при правке стори: после прохождения ревью обязательно писать "
        "комментарий при сохранении страницы — что поменялось.\n\n"
        f"## Источник (перенесено дословно)\n\n{q(paths[3:8])}", encoding="utf-8")
    own = paths[10]
    (kb / "Processes/Алгоритм-десять.md").write_text(
        f'---\ntitle: "Алгоритм десять"\nstatus: knowledge\ntype: process\nkind: knowledge\n'
        f'distilled: 2026-09-20\n{srcs_block([own] + paths[11:15])}---\n\n'
        "Алгоритм десять считает сумму налога по ставке десять процентов.\n\n"
        f"## Источник (перенесено дословно)\n\n### {own}\n\nАлгоритм десять.\n\n"
        + "".join(f"### {s}\n\n{TEMPLATE_PAR}\n\nЧужой расчёт {i}.\n\n"
                  for i, s in enumerate(paths[11:14]))
        + f"### {paths[14]}\n\n{TEMPLATE_PAR}\n\nЗдесь снова алгоритм десять: он же.\n",
        encoding="utf-8")
    card(root, "Concepts/Ссылается.md", "Смотри [[Комментарий-при-правке-стори|правило]].\n"
         "[[Комментарий-при-правке-стори]].\nИ дальше текст.", status="knowledge")
    man = kb / "meta/manifest.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"sources": {p: {"hash": "x", "cards": 1} for p in paths}},
                              ensure_ascii=False), encoding="utf-8")

    cp = run("kb_fix.py", "--template", "--apply", "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout[-800:] + cp.stderr[-400:]
    assert not (kb / "Processes/Комментарий-при-правке-стори.md").exists(), cp.stdout[-800:]
    assert (kb / "_archive/Комментарий-при-правке-стори.md").is_file()
    left = json.loads(man.read_text(encoding="utf-8"))["sources"]
    assert not set(paths[3:8]) & set(left), "источники карточки-шаблона не вернулись в план"
    assert set(paths[11:14]).isdisjoint(left), "отвязанные источники не вернулись в план"
    assert paths[14] in left and own in left, "тронуты источники, пришедшие по делу"

    alg = (kb / "Processes/Алгоритм-десять.md").read_text(encoding="utf-8")
    srcs = card_srcs(alg)
    assert srcs == [own, paths[14]], srcs
    assert "Чужой расчёт" not in alg and "Здесь снова алгоритм десять" in alg, alg
    assert "distilled: 2026-09-20" not in alg.split("\n---", 1)[0], "тезис не отправлен на переписывание"

    ref = (kb / "Concepts/Ссылается.md").read_text(encoding="utf-8")
    assert "[[Комментарий-при-правке-стори" not in ref, ref
    assert "Смотри правило." in ref and "\n[[" not in ref and "И дальше текст." in ref, ref


@test
def test_the_author_of_a_thesis_sees_repeated_knowledge_but_not_service_text(tmp: Path):
    """Автор тезиса видит повтор-знание, но не служебный текст шаблона.

    Повтор бывает инструкцией авторам и бывает правилом: «Отчёт состоит из ячеек…» стоит в
    34 постановках PRJ-A. Раскадровка прячет любой повтор, а тезис — только то, что модель
    один раз признала служебным; ответ хранится в кэше проекта и второй раз не спрашивается.
    """
    import importlib, json
    sys.path.insert(0, str(SCRIPTS))
    AG = importlib.import_module("agent_core")
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    paths = [p.relative_to(root).as_posix() for p in _template_mirror(root)]
    rule = ("Отчёт состоит из ячеек, значения которых заполняются статическим и динамическим "
            "текстом по правилам раздела форматов")
    for p in paths:
        f = root / p
        f.write_text(f.read_text(encoding="utf-8") + f"\n{rule}\n", encoding="utf-8")
    asked = []

    def fake(cfg, role, messages, **kw):
        text = messages[0]["content"]
        asked.append(text)
        nums = [line.split(".", 1)[0] for line in text.splitlines()
                if re.match(r"^\d+\. ", line) and ("ОБЯЗАТЕЛЬНО" in line or "Автор" in line)]
        return {"ok": True, "backend": 1, "model": "m", "tps": 9, "log": [],
                "text": json.dumps({"service": [int(n) for n in nums]})}

    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    service = R.service_template(cfg, str(root), call=fake)
    assert TEMPLATE_PAR in service and TEMPLATE_ROW in service, service
    assert rule not in service, "правило, повторённое в постановках, объявлено служебным"
    R.service_template(cfg, str(root), call=fake)
    assert len(asked) == 1, "служебность шаблона спрашивается у модели на каждом прогоне"
    quotes = f"### {paths[4]}\n\n{TEMPLATE_PAR}\n\n{rule}\n\nШаг первый.\n"
    shown = R.hide_quotes_template(quotes, [paths[4]], str(root), service)
    assert TEMPLATE_PAR not in shown and rule in shown and "Шаг первый" in shown, shown


@test
def test_a_service_section_is_named_by_the_whole_title_not_by_a_word_in_it(_t):
    """Служебная секция — по заголовку целиком, а не по куску слова.

    До 1.145.0 хватало «истори», «инструкц», «комментари»: служебными становились сущности
    «Core_История версии заявки», «ER_AS_История импорта», 67 секций «Уникальный
    идентификатор и историчность» и документ заказчика «Инструкция: подписание заявки с
    МЧД» — на PRJ-C 235 секций из 133 файлов, и модель выносила «пусто» о знании.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    R = importlib.import_module("agent_runner")
    for title in ("История изменений", "## История изменений страницы", "1. Оглавление",
                  "Содержание:", "Changelog", "Метаданные", "Комментарии"):
        assert R.is_service_section(title), f"служебная не опознана: {title}"
    for title in ("Core_История версии заявки", "ER_AS_История импорта",
                  "Уникальный идентификатор и историчность",
                  "Инструкция для Сервиса Заявителя: подписание заявки с МЧД",
                  "Основной сценарий №3. Работа с комментарием в карточке",
                  "Задача на разработку истории", "Текстовое содержание оповещений"):
        assert not R.is_service_section(title), f"знание объявлено служебным: {title}"


@test
def test_a_format_mask_in_the_text_does_not_make_a_page_a_template(tmp: Path):
    """Маска формата в тексте — спецификация, а не незаполненная форма.

    Правило «метка в тексте короче 4000 знаков — шаблон» сработало на трёх проектах 13 раз
    и все 13 — мимо: `ON_<КНД>_<УИД>.xml`, версия `XXX`, «Порядок формирования ИНН» с
    форматом `XXXXXX`. Такие алгоритмы уходили в Reference с пометкой «разбору не
    подлежит». Шаблон узнаётся по имени файла или по явной пометке в шапке; карточка
    «Шаблон проекта», заведённая по старому правилу, уходит в архив, а источник — в разбор.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    B = importlib.import_module("build_plan")
    spec = ("# Порядок формирования ИНН\n\nИНН имеет вид XXXXXX, файл — ON_<КНД>_<УИД>.xml, "
            "дата — yyyy-MM-dd. Проверка контрольного разряда обязательна.\n")
    assert not B.is_template("Sources/Confluence/Порядок_формирования_ИНН.md", spec)
    assert B.is_template("Sources/Confluence/Шаблон_протокола.md", "что угодно")
    assert B.is_template("Raw/x.md", "---\ntemplate: true\n---\nполе: ____\n")

    root = make_project(tmp)
    src = root / "Sources/Confluence/Порядок_формирования_ИНН.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(spec, encoding="utf-8")
    kb = root / "AuroraKnowledgeDB/Reference"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "Порядок-формирования-ИНН.md").write_text(
        '---\ntitle: "Порядок формирования ИНН"\nstatus: draft\ntype: reference\n'
        'kind: dictionary\nsources:\n  - "Sources/Confluence/Порядок_формирования_ИНН.md"\n'
        '---\n\n# Порядок формирования ИНН\n\n_Шаблон проекта. Разбору не подлежит: это форма, '
        'а не знание._\n', encoding="utf-8")
    cp = run("kb_fix.py", "--template", "--apply", "--allow-dirty", cwd=root)
    assert (root / "AuroraKnowledgeDB/_archive/Порядок-формирования-ИНН.md").is_file(), cp.stdout[-600:]
    plan = run("build_plan.py", cwd=root).stdout
    assert "Порядок_формирования_ИНН" in plan, f"спецификация формата не пошла в разбор:\n{plan[-600:]}"


@test
def test_an_empty_verdict_under_the_old_show_rules_is_judged_again_once(tmp: Path):
    """Вердикт «пусто» по прежним правилам показа пересматривается — один раз.

    Модель судила о знании по тому, что ей показали; до 1.145.0 от неё прятали секции
    «История версии заявки» и «Инструкция: подписание с МЧД» как служебные. Такие вердикты
    `--reopen` возвращает в план; вынесенный по новым правилам — остаётся на месте.
    """
    import importlib, json
    sys.path.insert(0, str(SCRIPTS))
    B = importlib.import_module("build_plan")
    root = make_project(tmp)
    src = root / "Sources/Confluence"
    src.mkdir(parents=True, exist_ok=True)
    for name in ("Старый.md", "Новый.md"):
        (src / name).write_text("# Core_История версии заявки\n\n" + "Атрибут. " * 60, encoding="utf-8")
    man = root / "AuroraKnowledgeDB/meta/manifest.json"
    man.parent.mkdir(parents=True, exist_ok=True)

    def rec(p, rule):
        r = {"hash": B.file_hash(str(src / p)), "processed": "2026-09-20", "cards": 0,
             "empty_reason": "Единственная секция помечена служебной"}
        if rule:
            r["empty_rule"] = rule
        return r
    man.write_text(json.dumps({"sources": {
        "Sources/Confluence/Старый.md": rec("Старый.md", 0),
        "Sources/Confluence/Новый.md": rec("Новый.md", B.EMPTY_RULE)}}), encoding="utf-8")
    out = run("build_plan.py", "--reopen", "--apply", cwd=root).stdout
    left = json.loads(man.read_text(encoding="utf-8"))["sources"]
    assert "Sources/Confluence/Старый.md" not in left, "вердикт по старым правилам не пересмотрен"
    assert "Sources/Confluence/Новый.md" in left, "свежий вердикт «пусто» пересматривается зря"
    assert "по прежним правилам показа" in out, out


@test
def test_a_code_is_never_translated_and_a_translated_code_gets_its_name_back(tmp: Path):
    """Код модели данных не переводится, а «переведённый» получает имя обратно.

    PRJ-C 30.09.2026: модель «перевела» `ER.Dop.Status` в «RYl:ЕР.Доп.Статус» (кириллические
    Е и Р — гомоглифы латинских), `ER.AS.Notice` — в «ER_AS_Извещение», и ремонт
    переименовал карточки. Код ER — имя сущности (Т-73, Т-74), переводить его нельзя.
    Там же переименование писало ссылки на строку словаря, а файл называло по правилу
    имён — 25 битых ссылок. И «не транслит» не запоминался: те же 15 имён уходили модели
    на каждом ремонте.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    TR = importlib.import_module("kb_translit")
    for code in ("ER.Dop.Status", "RYl-ER.Dop.Status", "ER-AS-Spe-ExciseAllTotal", "IsAnnulated",
                 "Status-REGISTERED", "ER.DNsp"):
        assert TR.is_identifier(code) and not TR.is_latin_name(code), code
    assert TR.is_latin_name("SPR-002-Tipy-Transportnogo-sredstva")

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    card(root, "Concepts/RYl-ЕР.Доп.Статус.md", "Статус заявки в модели данных.",
         status="knowledge", aliases='\n  - "RYl-ER.Dop.Status"')
    card(root, "Concepts/Profil-abonenta.md", "Профиль обслуживания абонента.", status="draft")
    card(root, "Concepts/Ссылки.md", "См. [[RYl:ЕР.Доп.Статус]], [[Profil-abonenta]] и "
         "[[Профиль абонента]].", status="knowledge")
    d = kb / "meta/translit.md"
    d.parent.mkdir(parents=True, exist_ok=True)
    TR.write_dict({"RYl-ER.Dop.Status": "RYl:ЕР.Доп.Статус", "Profil-abonenta": "Профиль абонента",
                   "Processes": TR.NOT_TRANSLIT}, str(d))
    assert TR.read_dict(str(d))["Processes"] == TR.NOT_TRANSLIT, "вердикт «не транслит» не читается"
    run("kb_translit.py", "--rename", "--apply", "--allow-dirty", cwd=root)
    back = kb / "Concepts/RYl-ER.Dop.Status.md"
    assert back.is_file(), sorted(p.name for p in (kb / "Concepts").iterdir())
    assert 'title: "RYl-ER.Dop.Status"' in back.read_text(encoding="utf-8")
    assert (kb / "Concepts/Профиль-абонента.md").is_file(), "настоящий транслит не переименован"
    links = (kb / "Concepts/Ссылки.md").read_text(encoding="utf-8")
    assert "[[RYl-ER.Dop.Status]]" in links, links
    assert "[[Профиль-абонента]]" in links and "[[Профиль абонента]]" not in links, \
        f"ссылка ведёт на строку словаря, а не на имя файла:\n{links}"
    assert TR.read_dict(str(d))["RYl-ER.Dop.Status"] == TR.NOT_TRANSLIT, "код остался в словаре переводом"

    R = importlib.import_module("agent_runner")
    run_src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8").split(
        "def run_translit(")[1].split("def report_translit(")[0]
    assert "KT.NOT_TRANSLIT" in run_src, "вердикт модели «не транслит» не записывается в словарь"


@test
def test_an_archived_copy_and_project_forms_are_not_alias_rivals(tmp: Path):
    """Копия карточки в архиве и шаблоны проекта — не соперники по синониму.

    Ремонт объявлял спором карточку и её же копию в `_archive` («Epic-4.2», «Личный
    кабинет»), а три версии шаблона `Templates/test_review` — спором синонимов базы: модель
    тратила вызовы, чтобы ответить «дубль — человеку», и каждый шаг ремонта завершался
    кодом 1. Ссылка, которой не на что указывать, — работа `--stubs`, а не сбой шага.
    """
    root = make_project(tmp)
    card(root, "Concepts/Формирование-заявки.md", "Эпик формирования. [[Нет-такой-карточки]]",
         status="knowledge", aliases='\n  - "Epic-4.2"')
    card(root, "_archive/Формирование-заявки.md", "Старая копия.", status="deprecated",
         aliases='\n  - "Epic-4.2"')
    tpl = root / "Templates"
    tpl.mkdir(exist_ok=True)
    for v in ("1.0", "1.1"):
        (tpl / f"test_review_v{v}.md").write_text(
            f'---\ntitle: "Ревью {v}"\naliases:\n  - "Ревью тест-кейса"\n---\n\nФорма.\n',
            encoding="utf-8")
    cp = run("kb_fix.py", "--all", "--apply", "--allow-dirty", cwd=root)
    assert "«Epic-4.2»" not in cp.stdout, cp.stdout[-800:]
    assert "Ревью тест-кейса" not in cp.stdout, "шаблоны проекта стали карточками базы"
    assert cp.returncode == 0, f"ссылка без карточки красит ремонт упавшим: rc={cp.returncode}"


@test
def test_a_link_to_a_source_page_leads_to_the_card_of_its_entity(tmp: Path):
    """Ссылка на страницу-источник по полному имени ведёт в карточку её сущности.

    `[[US-6.5.4._Получение_статуса_приёма_пакета_по_API]]` — имя страницы US; карточка
    процесса названа сущностью. `--stubs` заводил под такую ссылку заготовку рядом с живой
    карточкой (PRJ-C 30.09.2026: двойников 17 → 19 за один ремонт).
    """
    root = make_project(tmp)
    card(root, "Processes/Получение-статуса-приёма-пакета-по-API.md",
         "Учётная система получает статус приёма пакета.", status="knowledge")
    card(root, "Systems/Интеграция.md", "См. [[US-6.5.4._Получение_статуса_приёма_пакета_по_API]].",
         status="knowledge")
    run("kb_fix.py", "--links", "--stubs", "--apply", "--allow-dirty", cwd=root)
    text = (root / "AuroraKnowledgeDB/Systems/Интеграция.md").read_text(encoding="utf-8")
    assert "[[Получение-статуса-приёма-пакета-по-API" in text, text
    made = [p.name for p in (root / "AuroraKnowledgeDB").rglob("*.md") if "_статуса_" in p.name
            or p.name.startswith("Получение_")]
    assert not made, f"под ссылку заведён двойник: {made}"


@test
def test_a_human_correction_is_named_by_its_path_not_by_a_broken_link(tmp: Path):
    """Исправление человека называется путём к файлу, а не вики-ссылкой.

    Файл исправления лежит в `Raw/corrections/`, вне базы: ссылка `[[…]]` на него в Obsidian
    не открывается, и линтер звал её битой — 12 «битых ссылок» PRJ-C после каждого ремонта.
    Смена одного лишь вида ссылки тезис не сбрасывает: слово человека то же.
    """
    root = make_project(tmp)
    card(root, "Concepts/Заявка.md", "У заявки четыре статуса.", status="knowledge",
         sources='\n  - "Sources/Confluence/Заявки.md"', distilled="2026-09-20")
    fix = root / "Raw/corrections"
    fix.mkdir(parents=True, exist_ok=True)
    (fix / "Статусов-пять.md").write_text(
        '---\ntitle: "Статусов пять"\ncorrects: "[[Заявка]]"\ncreated: 2026-09-21\n---\n\n'
        "Статусов пять: добавлен «Отозвана».\n", encoding="utf-8")
    run("kb_corrections.py", "--apply", cwd=root)
    text = (root / "AuroraKnowledgeDB/Concepts/Заявка.md").read_text(encoding="utf-8")
    assert "Источник исправления: `Raw/corrections/Статусов-пять.md`" in text, text
    assert 'corrected_by: "Статусов-пять"' in text, text
    # старый вид ссылки → новый: переписать, но тезис не сбрасывать
    old = text.replace("`Raw/corrections/Статусов-пять.md`", "[[Статусов-пять]]").replace(
        "updated:", "distilled: 2026-09-22\nupdated:", 1)
    (root / "AuroraKnowledgeDB/Concepts/Заявка.md").write_text(old, encoding="utf-8")
    run("kb_corrections.py", "--apply", cwd=root)
    again = (root / "AuroraKnowledgeDB/Concepts/Заявка.md").read_text(encoding="utf-8")
    assert "`Raw/corrections/Статусов-пять.md`" in again and "[[Статусов-пять]]" not in again
    assert "distilled: 2026-09-22" in again, "смена вида ссылки сбросила тезис"
    lint = run("kb_lint.py", cwd=root).stdout
    broken = lint.split("## битые ссылки", 1)[1].split("\n## ", 1)[0] if "## битые ссылки" in lint else ""
    assert "Статусов-пять" not in broken, broken


@test
def test_a_document_is_parsed_once_even_when_its_copy_is_named_differently(tmp: Path):
    """Бумага разбирается один раз, даже если копию человек назвал по-своему.

    Правило «есть копия — машинную расшифровку не разбирать» узнавало копию по имени
    буквально. «gf_дКН_представление.md» (копия человека) рядом с «gf_дКН представление.md»
    (расшифровка с `converted_from:`) не узнавалась, и одна бумага разбиралась дважды:
    шесть документов PRJ-C, госконтракт — 6 + 6 карточек. Ремонт `--copies` снимает след.
    """
    import importlib, json
    sys.path.insert(0, str(SCRIPTS))
    B = importlib.import_module("build_plan")
    root = make_project(tmp)
    d = root / "Raw/customer/FAQ"
    d.mkdir(parents=True, exist_ok=True)
    human = d / "Памятка_для_перевозчиков.md"
    human.write_text("# Памятка для перевозчиков\n\n> **Источник:** `Памятка для перевозчиков.pdf`\n\n"
                     + "Перевозчик предъявляет QR-код на границе. " * 20, encoding="utf-8")
    machine = d / "Памятка для перевозчиков.md"
    machine.write_text('---\ntitle: "Памятка"\nconverted_from: "Raw/customer/FAQ/Памятка для '
                       'перевозчиков.pdf"\n---\n\n' + "Перевозчик предъявляет код. " * 20,
                       encoding="utf-8")
    old = d / "Старый.converted.md"
    old.write_text("расшифровка " * 40, encoding="utf-8")
    (d / "Старый.md").write_text("копия человека " * 40, encoding="utf-8")
    rel = lambda p: p.relative_to(root).as_posix()
    assert B.human_twin(str(machine)).endswith("Памятка_для_перевозчиков.md")
    assert B.human_twin(str(old)).endswith("Старый.md")
    assert not B.human_twin(str(human)), "копия человека объявлена машинной"
    plan = run("build_plan.py", cwd=root).stdout
    assert "Памятка_для_перевозчиков" in plan and "Памятка для перевозчиков.md" not in plan, plan[-500:]

    kb = root / "AuroraKnowledgeDB"
    card(root, "Processes/Предъявление-кода.md",
         f"Перевозчик предъявляет код.\n\n## Источник (перенесено дословно)\n\n### {rel(human)}\n\n"
         f"Из копии.\n\n### {rel(machine)}\n\nИз расшифровки.\n",
         status="knowledge", kind="knowledge", distilled="2026-09-20",
         sources=f'\n  - "{rel(human)}"\n  - "{rel(machine)}"')
    card(root, "Concepts/Только-из-расшифровки.md", "Знание из расшифровки.", status="knowledge",
         sources=f'\n  - "{rel(machine)}"')
    card(root, "Concepts/Ссылается.md", "См. [[Только-из-расшифровки|памятку]].", status="knowledge")
    man = kb / "meta/manifest.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"sources": {rel(human): {"cards": 1}, rel(machine): {"cards": 2}}},
                              ensure_ascii=False), encoding="utf-8")
    cp = run("kb_fix.py", "--copies", "--apply", "--allow-dirty", cwd=root)
    kept = (kb / "Processes/Предъявление-кода.md").read_text(encoding="utf-8")
    assert card_srcs(kept) == [rel(human)], card_srcs(kept)
    assert "Из расшифровки" not in kept and "Из копии" in kept and "distilled: 2026-09-20" in kept, kept
    assert (kb / "_archive/Только-из-расшифровки.md").is_file(), cp.stdout[-600:]
    assert "См. памятку." in (kb / "Concepts/Ссылается.md").read_text(encoding="utf-8")
    assert rel(machine) not in json.loads(man.read_text(encoding="utf-8"))["sources"]


@test
def test_a_meeting_table_is_cut_between_rows_with_its_header(tmp: Path):
    """Протокол встречи — таблица, и режется он между строками, с шапкой в каждом куске.

    Реплика длиннее 3000 знаков резалась по концам предложений, а точка стоит внутри
    ячейки: протокол рвался посреди строки, и карточка пункта 4 получила пункты 9–12 и
    обрывок 8-го (PRJ-C). Стенограммы, нарезанные прежним правилом, `--reopen` возвращает.
    """
    import importlib, json
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    rows = "\n".join(f"| {i} | Сделать задачу номер {i}. Проверить на стенде. | Реестр | "
                     f"Подробности {i}: всё описано в протоколе. | Отдел |" for i in range(1, 40))
    raw = "Источник: протокол встречи.\n\n| № | Задача | Форма | Детали | Кто |\n|---|---|---|---|---|\n" + rows
    turns = A.meeting_turns(raw)
    assert len(turns) > 2, turns
    for t_ in turns[1:]:
        lines = t_.split("\n")
        assert lines[0].startswith("| № |") and lines[1].startswith("|---"), lines[:2]
        assert all(l.startswith("|") and l.rstrip().endswith("|") for l in lines), \
            f"строка таблицы разрезана:\n{t_[:300]}"
    assert A.meeting_has_long_table(raw)

    root = make_project(tmp)
    m = root / "Raw/meetings/action_items_2026-03-24.md"
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text(raw, encoding="utf-8")
    card(root, "Requirements/Пункт.md", "Пункт протокола.", status="draft",
         sources='\n  - "Raw/meetings/action_items_2026-03-24.md"')
    B = importlib.import_module("build_plan")
    man = root / "AuroraKnowledgeDB/meta/manifest.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"sources": {"Raw/meetings/action_items_2026-03-24.md": {
        "hash": B.file_hash(str(m)), "cards": 1}}}), encoding="utf-8")
    run("build_plan.py", "--reopen", "--apply", cwd=root)
    left = json.loads(man.read_text(encoding="utf-8"))["sources"]
    assert "Raw/meetings/action_items_2026-03-24.md" not in left, "встреча с прежней нарезкой не вернулась"


@test
def test_an_extracted_definition_leaves_no_empty_sentence(tmp: Path):
    """Вынос определения не оставляет пустого предложения.

    На месте определения оставалось «Заявители — [[Заявители]].» или голая «[[ДОК]]», а
    связывание делало «Заявители — Заявители.» (PRJ-C 6, PRJ-A 6, PRJ-B 3). Такое предложение
    уходит; термин получает ссылку там, где тезис его упоминает. Ремонт `--tautologies`
    снимает оставшиеся.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    A = importlib.import_module("aurora_common")
    text = ("Участники — [[Заявители|заявители]] и перевозчики.\nЗаявители — Заявители.\n"
            "[[ДОК]]\nОсновной документ — [[Основной-документ]].\n"
            "Перевозчики — Перевозчики-определение.\nНДС — налог на добавленную стоимость.")
    got, n = A.drop_echo_sentences(text)
    assert n == 4, got
    assert "Участники — [[Заявители|заявители]]" in got and "НДС — налог" in got, got

    root = make_project(tmp)
    card(root, "Roles/Участники.md", "Участники — заявители.\nЗаявители — [[Заявители]].\n\n"
         "## Источник (перенесено дословно)\n\nЗаявители — Заявители.\n",
         status="knowledge", kind="knowledge")
    run("kb_fix.py", "--tautologies", "--apply", "--allow-dirty", cwd=root)
    after = (root / "AuroraKnowledgeDB/Roles/Участники.md").read_text(encoding="utf-8")
    own, quotes = after.split("## Источник (перенесено дословно)")
    assert "Заявители — [[Заявители]]" not in own and "Участники — заявители." in own, own
    assert "Заявители — Заявители." in quotes, "тронут дословный раздел"
    R = importlib.import_module("agent_runner")
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8").split(
        "def apply_extract_plan(")[1].split("\ndef ")[0]
    assert "drop_echo_sentences(new_thesis)" in src, "вынос определения снова оставляет пустую фразу"


@test
def test_returning_a_code_name_checks_the_tree_before_writing(tmp: Path):
    """Возврат имени коду пишет только после охраны дерева — и доводит ссылки до конца.

    В 1.145.0 коды переименовывались ДО проверки git: охрана видела эти же переименования
    грязным деревом, отказывала (код 2), и маршрут «Починить базу» падал с карточками под
    старыми именами и ссылками на перевод (PRJ-C 30.09.2026).
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    TR = importlib.import_module("kb_translit")
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    card(root, "Concepts/ЕР.Док.БанкИд.md", "Идентификатор банка.", status="knowledge",
         aliases='\n  - "ER.Doc.BankId"\n  - "ЕР.Док.БанкИд"')
    card(root, "Concepts/Ссылки.md", "См. [[ЕР.Док.БанкИд]].", status="knowledge")
    (kb / "meta").mkdir(parents=True, exist_ok=True)
    TR.write_dict({"ER.Doc.BankId": "ЕР.Док.БанкИд"}, str(kb / "meta/translit.md"))
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "база"], cwd=root, capture_output=True)
    cp = run("kb_translit.py", "--rename", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout[-600:] + cp.stderr[-300:]
    back = kb / "Concepts/ER.Doc.BankId.md"
    assert back.is_file(), sorted(p.name for p in (kb / "Concepts").iterdir())
    assert "ЕР.Док.БанкИд" not in back.read_text(encoding="utf-8"), "синоним-перевод остался"
    assert "[[ER.Doc.BankId]]" in (kb / "Concepts/Ссылки.md").read_text(encoding="utf-8")


@test
def test_a_route_that_made_the_base_worse_says_so(tmp: Path):
    """Маршрут, после которого база стала хуже, не отчитывается «ошибок не было».

    «Починить базу» на PRJ-C 30.09.2026: линтер 34 → 53, а итог — «Ошибки: не было»: остаток
    перезаписывался до сравнения, и новое всегда было нулём. Теперь новые ошибки считаются
    до перезаписи и уходят в итог маршрута ошибкой. И код шага говорит правду: `distill`
    без работы — успех, а не код 1 в каждом обороте.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    RS = importlib.import_module("run_summary")
    root = make_project(tmp)
    card(root, "Concepts/Опора.md", "Опора базы.", status="knowledge")
    first = run("kb_lint.py", "--residue", cwd=root).stdout
    assert "нового после починки: 0" in first, first
    card(root, "Concepts/Новая.md", "См. [[Нет-такой-карточки]].", status="knowledge")
    again = run("kb_lint.py", "--residue", cwd=root).stdout
    got = RS.merge(RS.parse(again.splitlines()))
    assert any("база хуже" in k for k in got["errors"]), f"ухудшение не дошло до итога: {again}"
    lines = RS.render(got)
    assert "Ошибки: не было" not in "\n".join(lines), lines
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert 'return 0 if made and not res["unsupported"] else 1' not in src, \
        "distill снова красит шаг упавшим, когда работы не было"


@test
def test_the_sync_skips_pages_whose_version_did_not_change(tmp: Path):
    """Синк не качает страницу, чья версия не менялась, — и обходит корень один раз.

    PRJ-C 29.09.2026: 28 минут на 1202 страницы ради 30 изменений — каждая качалась телом и
    конвертировалась, хотя номер версии приходит в списке детей бесплатно. Там же один
    корень, записанный в настройке дважды, обходился дважды (1202 записи вместо 1058).
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    CE = importlib.import_module("confluence_export")
    root = make_project(tmp)
    mirror = root / "Sources/Confluence"
    ver = {"10": 1, "11": 1, "12": 1}
    tree = {"10": ("Корень", ["11", "12"]), "11": ("Первая", []), "12": ("Вторая", [])}
    fetched = []

    class Api:
        def page(self, pid):
            fetched.append(pid)
            return {"title": tree[pid][0], "version": {"number": ver[pid], "when": "2026-09-30"},
                    "body": {"storage": {"value": f"<p>Страница {pid}, версия {ver[pid]}.</p>"}},
                    "space": {"key": "S"}, "_links": {"webui": f"/p/{pid}"}}

        def children(self, pid):
            return [{"id": c, "title": tree[c][0], "version": {"number": ver[c]}}
                    for c in tree[pid][1]]

        def attachments(self, pid):
            return {}

        def user_name(self, key):
            return key

    here = os.getcwd()
    os.chdir(root)
    try:
        first = CE.Exporter(Api(), str(mirror), "https://wiki", "S", False)
        for r in ("10", "10"):
            first.walk(r, [])
        first.save_cache()
        assert sorted(fetched) == ["10", "11", "12"], f"корень обойдён дважды: {fetched}"
        assert (root / ".opencode/cache/confluence_pages.json").is_file()

        fetched.clear()
        ver["12"] = 2
        second = CE.Exporter(Api(), str(mirror), "https://wiki", "S", False)
        second.walk("10", [])
        assert sorted(fetched) == ["10", "12"], \
            f"неизменённая страница скачана заново или изменённая пропущена: {fetched}"
        assert {r[0] for r in second.records} == {"10", "11", "12"}, "пропущенная страница выпала из состояния"
        assert "версия 2" in (mirror / "Корень/Вторая.md").read_text(encoding="utf-8")

        fetched.clear()
        forced = CE.Exporter(Api(), str(mirror), "https://wiki", "S", True)
        forced.walk("10", [])
        assert sorted(fetched) == ["10", "11", "12"], "--force не прошёл зеркало полностью"
    finally:
        os.chdir(here)
    src = (SCRIPTS / "confluence_export.py").read_text(encoding="utf-8")
    assert "dict.fromkeys(str(r) for r in roots)" in src, "повтор корня снова обходится дважды"


@test
def test_a_backslash_in_a_web_link_means_what_the_browser_reads(_t):
    """Обратная косая в адресе сайта — прямая, как её читает браузер.

    Вложения `\\images_ca\\icons\\*.png` со страницы налоговой службы шли в 404: `urljoin`
    оставлял косые как есть (PRJ-C 29.09.2026).
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    W = importlib.import_module("web_export")
    got = W.web_join("https://example.ru/rn77/service/", "\\images_ca\\icons\\qr.png")
    assert got == "https://example.ru/images_ca/icons/qr.png", got
    assert W.web_join("https://example.ru/a/b.html", "c.png") == "https://example.ru/a/c.png"


@test
def test_a_card_whose_source_left_the_mirror_is_repaired(tmp: Path):
    """Источник карточки, которого нет на диске, чинится ремонтом, а не висит в отчёте.

    PRJ-C: 19 карточек называли в `sources` файлы, которых нет, — `ops:stats` сообщал о них
    каждый прогон. Переехавшая страница находится по коду документа, испорченная запись
    (два пути через «;», текст вместо пути) исправляется, пропавшая — снимается с записью в
    истории; дословный текст остаётся.
    """
    root = make_project(tmp)
    ref = root / "Sources/Confluence/НСИ"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "SPR-012_Роль_пользователя.md").write_text("# SPR-012 Роль\n\nтекст\n", encoding="utf-8")
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    (root / "Raw/project/A.md").write_text("а" * 300, encoding="utf-8")
    (root / "Raw/project/B.md").write_text("б" * 300, encoding="utf-8")
    card(root, "Reference/Роль.md",
         "Роль пользователя.\n\n## Источник (перенесено дословно)\n\n"
         "### Sources/Confluence/НСИ/SPR-012_Rol_polzovatelia.md\n\nтекст\n",
         status="knowledge", sources='\n  - "Sources/Confluence/НСИ/SPR-012_Rol_polzovatelia.md"')
    card(root, "Concepts/Склейка.md", "Знание.", status="knowledge",
         sources='\n  - "Raw/project/A.md; Raw/project/B.md"\n  - "Raw/dictionaries (legacy, перенесены)"')
    card(root, "Concepts/Ушедшая.md", "Знание страницы.", status="knowledge",
         sources='\n  - "Sources/Confluence/Удалена.md"')
    run("kb_fix.py", "--gone-sources", "--apply", "--allow-dirty", cwd=root)
    kb = root / "AuroraKnowledgeDB"
    role = (kb / "Reference/Роль.md").read_text(encoding="utf-8")
    assert card_srcs(role) == ["Sources/Confluence/НСИ/SPR-012_Роль_пользователя.md"], card_srcs(role)
    assert "### Sources/Confluence/НСИ/SPR-012_Роль_пользователя.md" in role, "блок не переименован"
    glued = (kb / "Concepts/Склейка.md").read_text(encoding="utf-8")
    assert card_srcs(glued) == ["Raw/project/A.md", "Raw/project/B.md"], card_srcs(glued)
    gone = (kb / "Concepts/Ушедшая.md").read_text(encoding="utf-8")
    assert card_srcs(gone) == [] and "источника больше нет в зеркале" in gone, gone
    assert "Знание страницы." in gone, "знание пропало вместе с путём"


@test
def test_a_repeated_critic_dispute_ends_with_the_checked_plan(tmp: Path):
    """Спор исполнителя с критиком о том же тексте не повторяется вечно.

    US-3.6.21 PRJ-C отклонялась критиком одной и той же фразой в оборотах 2, 3, 4, и маршрут
    кончался застоем. Отказ критика теперь учитывается; на второй встрече с тем же текстом
    принимается разбор, прошедший проверку арифметикой, а возражение остаётся в заметке.
    """
    import importlib, json
    sys.path.insert(0, str(SCRIPTS))
    R = importlib.import_module("agent_runner")
    BP = importlib.import_module("build_plan")
    root = make_project(tmp)
    src = root / "Sources/Confluence/US-1.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# US-1\n\n" + "Текст истории. " * 40, encoding="utf-8")
    assert not R.critic_disputed_before(str(root), "Sources/Confluence/US-1.md")
    fail = root / BP.FAILURES
    fail.parent.mkdir(parents=True, exist_ok=True)
    fail.write_text(json.dumps({"Sources/Confluence/US-1.md": {
        "hash": BP.file_hash(str(src)), "count": 1, "note": "объединяет все секции",
        "critic": True}}, ensure_ascii=False), encoding="utf-8")
    assert R.critic_disputed_before(str(root), "Sources/Confluence/US-1.md")
    src.write_text(src.read_text(encoding="utf-8") + "Правка.", encoding="utf-8")
    assert not R.critic_disputed_before(str(root), "Sources/Confluence/US-1.md"), \
        "спор о прежнем тексте засчитан новому"
    code = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert "if not from_check and critic_disputed_before(cwd, source):" in code


@test
def test_card_links_come_fast_and_the_same(tmp: Path):
    """Связи по глоссарию ставятся так же, но без сотен тысяч регулярных выражений.

    `kb:links` на PRJ-A — 42 с, из них 34 с на 225 тысяч поисков «термин × карточка»; каждый
    оборот маршрута, даже пустой. Сначала поиск подстроки, потом граница слова: 38 с → 3 с,
    вывод байт в байт тот же.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    G = importlib.import_module("kb_graph")
    root = make_project(tmp)
    card(root, "Glossary/Налоговая-декларация.md", "Декларация — документ.", status="knowledge")
    card(root, "Concepts/Подача.md", "Подача: налоговая декларация уходит в срок.", status="knowledge")
    card(root, "Concepts/Декларант.md", "Налоговая декларацияхх — не то слово.", status="knowledge")
    pairs = G.glossary_links(str(root / "AuroraKnowledgeDB"))
    got = {(os.path.basename(a), os.path.basename(b)) for a, b in pairs}
    assert ("Подача.md", "Налоговая-декларация.md") in got, got
    assert ("Декларант.md", "Налоговая-декларация.md") not in got, "граница слова потеряна"


@test
def test_the_panel_is_served_as_one_document_assembled_from_its_parts(tmp: Path):
    """Оболочка, стили и скрипт лежат в трёх файлах, а в браузер уходит один документ.

    Если подстановка не сработает, человек откроет панель без стилей или без скрипта —
    пустую страницу, которую по тексту `index.html` не заметить.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    page = ck.ui_source()
    assert "@include" not in page, "в странице остался маркер подстановки"
    assert page == ui_source(), "сервер и проверки собирают страницу по-разному"
    assert page.count("<style>") == 1 and page.count("<script>") == 1 \
        and "const UI_VERSION" in page and ".drawer{" in page, "в странице нет стилей или скрипта"
    shell = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert len(shell.splitlines()) < 400, "оболочка снова вобрала в себя стили и скрипт"
    assert ck.ui_version() == (KIT / "VERSION").read_text(encoding="utf-8").strip(), \
        "версия панели не читается из подставленного скрипта"


@test
def test_the_interface_catalogues_agree_with_the_panel(tmp: Path):
    """`kit_i18n.py --check` зелёная: каждая надпись панели есть в каталоге и наоборот.

    CONTRIBUTING требует её зелёной перед выпуском, но проверку никто не гонял, и она
    краснела на сорока ключах справки: `data-help="ключ"` просит `ключ.what/.how/.result`,
    а проверка искала голый «ключ». Красное, к которому привыкли, перестаёт быть сигналом.
    """
    cp = subprocess.run([sys.executable, str(KIT / "scripts" / "kit_i18n.py"), "--check"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert cp.returncode == 0, cp.stdout[-1500:] + cp.stderr[-500:]


@test
def test_one_status_scale_orders_merges_indexes_and_maps(tmp: Path):
    """Знание выше черновика везде: в слиянии двойников, оглавлении, карте и пакете.

    Шкала доверия сменилась в 1.89 (`knowledge` вместо ступеней приёмки), а таблицы рангов
    остались по две-три штуки: в `kb_fix` она не знала `knowledge`, и `kb:dedupe --merge-all`
    оставлял длинный черновик, вливая в него короткое знание; в оглавлении знание шло после
    черновиков; в каждой карте оно было подписано «не проверено».
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    ranks = [AC.status_rank(s) for s in ("knowledge", "draft", "", "placeholder", "deprecated")]
    assert ranks[0] > ranks[1] > ranks[2] > ranks[3] == ranks[4] == 0, ranks
    assert AC.status_rank("verified") == AC.status_rank("knowledge"), "легаси-статус потерял вес"
    assert AC.status_rank('"knowledge"') == AC.status_rank("knowledge"), "кавычки в шапке"

    root = make_project(tmp, git=True)
    old = os.getcwd()
    os.chdir(root)
    try:
        K = importlib.import_module("kb_fix")
        def mk(stem: str, status: str, body: str):
            path = f"AuroraKnowledgeDB/Concepts/{stem}.md"
            return path, K.Card(path, f'---\ntitle: "{stem}"\nstatus: {status}\n---\n\n# {stem}\n\n{body}\n')
        a, b = mk("Платёж", "knowledge", "Короткий тезис."), mk("Платеж", "draft", "Длинный тезис. " * 20)
        winner, losers, why = K.pick_winner(dict([a, b]), [a[0], b[0]], {})
    finally:
        os.chdir(old)
    assert winner == a[0] and why == "статус", f"слияние оставило черновик: {winner} ({why})"

    kb = root / "AuroraKnowledgeDB"
    card(root, "Concepts/Аргумент.md", "Тезис черновика.", status="draft")
    card(root, "Concepts/Бумага.md", "Тезис знания.", status="knowledge")
    card(root, "Concepts/Вексель.md", "Тезис импорта.", status="imported")
    run("kb_index.py", "--apply", "--section", "Concepts", cwd=root)
    rows = [l for l in (kb / "Concepts/_index.md").read_text(encoding="utf-8").splitlines()
            if l.startswith("| [[")]
    order = [re.match(r"\| \[\[([^\]]+)\]\]", l).group(1) for l in rows]
    assert order == ["Бумага", "Аргумент", "Вексель"], f"знание не первым в оглавлении: {order}"

    run("kb_moc.py", "--apply", "--allow-dirty", cwd=root)
    lines = [l for f in (kb / "MOC").glob("*.md") for l in f.read_text(encoding="utf-8").splitlines()
             if "[[Бумага|" in l or "[[Аргумент|" in l]
    assert any("[[Бумага|" in l and "не проверено" not in l for l in lines), lines
    assert any("[[Аргумент|" in l and "не проверено" in l for l in lines), lines


@test
def test_mcp_search_survives_parallel_calls_and_ignores_the_archive(tmp: Path):
    """Параллельные `kb_search` не мешают друг другу, а снятое в `_archive` не находится.

    С 1.147.7 вызовы идут по потокам, а поиск меняет каталог процесса: первый закончивший
    возвращал его на место под ногами у остальных, и шестая часть вызовов отвечала
    «база ничего не знает». Архив же (слитые двойники, заглушки) читался как знание.
    """
    root = make_project(tmp)
    card(root, "Concepts/Бумага.md", "Зебракадабра — понятие, которое знает база.", status="knowledge")
    arch = root / "AuroraKnowledgeDB" / "_archive"
    arch.mkdir(exist_ok=True)
    (arch / "Старый-двойник.md").write_text(
        '---\ntitle: "Старый двойник"\ntype: concept\nstatus: knowledge\n---\n\n'
        "Зебракадабра: слитая и снятая карточка.\n", encoding="utf-8")

    calls = 48
    lines = [json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})]
    lines += [json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {
        "name": "kb_search", "arguments": {"query": "зебракадабра", "limit": 5}}})
        for i in range(1, calls + 1)]
    lines.append(json.dumps({"jsonrpc": "2.0", "id": 900, "method": "tools/call", "params": {
        "name": "kb_card", "arguments": {"name": "Старый-двойник"}}}))
    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_mcp.py"), "--project", str(root)],
                        input="\n".join(lines) + "\n", capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=180, cwd=str(tmp))
    answers = {}
    for line in cp.stdout.splitlines():
        msg = json.loads(line)
        if msg["id"] not in (0,):
            answers[msg["id"]] = msg["result"]["content"][0]["text"]
    searches = [t for i, t in answers.items() if i != 900]
    assert len(searches) == calls, f"ответили не все: {len(searches)} из {calls}\n{cp.stderr[-500:]}"
    bad = [t for t in searches if not t.startswith("Найдено карточек: 1")]
    assert not bad, f"{len(bad)} из {calls} параллельных поисков ответили не то: {bad[0][:200]}"
    assert all("Бумага" in t and "Старый-двойник" not in t for t in searches), \
        "в выдаче архивная карточка"
    assert "в базе нет" in answers[900], "kb_card отдала карточку из архива"

    sys.path.insert(0, str(SCRIPTS))
    import importlib
    old = os.getcwd()
    os.chdir(root)
    try:
        P = importlib.import_module("ctx_pack")
        assert "Старый-двойник" not in P.load_cards(), "ctx_pack читает архив"
    finally:
        os.chdir(old)


@test
def test_repair_does_not_create_code_stubs_that_it_archives_next(tmp: Path):
    """`--stubs` не заводит заготовку под код артефакта, которую унесёт `--drop-code-stubs`.

    Шаг «Завести заготовки» пропускал голые коды, а «убрать заготовки под коды» брал ещё и
    коды с приставкой проекта: маршрут «Починить базу» каждый прогон писал
    `RU.PRJ.US-3.2.5` и тут же архивировал.
    """
    root = make_project(tmp, git=True)
    card(root, "Concepts/Тест-ссылки.md",
         "Связано: [[RU.PRJ.US-3.2.5]], [[US-3.1.1]], [[Новое понятие X]].", status="draft")
    run("kb_fix.py", "--stubs", "--apply", "--allow-dirty", cwd=root, expect_rc=None)
    kb = root / "AuroraKnowledgeDB"
    assert (kb / "Concepts/Новое-понятие-X.md").is_file(), "обычная заготовка не заведена"
    assert not (kb / "Concepts/RU.PRJ.US-3.2.5.md").exists(), "заготовка под код с приставкой заведена"
    cp = run("kb_fix.py", "--drop-code-stubs", "--apply", "--allow-dirty", cwd=root, expect_rc=None)
    assert "0 в архив" in cp.stdout, f"второй шаг нашёл, что убрать:\n{cp.stdout[:500]}"


@test
def test_generated_files_are_not_rewritten_for_the_date_alone(tmp: Path):
    """Индекс и карта не переписываются, если изменилась одна дата сборки.

    kb:index, kb:moc и kb:trace-table каждый новый день переписывали файлы ради строки
    «обновлено …»: в git сотни правок без единой новой ссылки.
    """
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import undated, write_if_changed
    f = tmp / "map.md"
    text = "---\nupdated: 2026-01-02\n---\n\n_Карточек: 3 · собрано 2026-01-02_\n"
    assert write_if_changed(str(f), text), "новый файл не записан"
    newer = text.replace("2026-01-02", "2026-03-04")
    assert not write_if_changed(str(f), newer), "переписали ради одной даты"
    assert f.read_text(encoding="utf-8") == text
    assert write_if_changed(str(f), newer.replace("Карточек: 3", "Карточек: 4")), \
        "изменение содержимого не записано"
    j = '{\n "date": "2026-01-02",\n "tasks": 3\n}'
    assert undated(j) == undated(j.replace("2026-01-02", "2026-05-05")) != undated(j.replace("3", "4"))

    root = make_project(tmp, git=True)
    card(root, "Concepts/Бумага.md", "Тело.", status="knowledge")
    run("kb_index.py", "--root-index", "--apply", cwd=root, expect_rc=0)
    kb = root / "AuroraKnowledgeDB"
    files = [kb / "index.md", kb / "Concepts/_index.md"]
    stale = {}
    for p in files:
        t = p.read_text(encoding="utf-8")
        old = re.sub(r"обновлено \S+", "обновлено 2020-01-01", t)
        assert old != t, f"в {p.name} нет даты сборки"
        p.write_text(old, encoding="utf-8")
        stale[p] = old
    run("kb_index.py", "--root-index", "--apply", cwd=root, expect_rc=0)
    for p, old in stale.items():
        assert p.read_text(encoding="utf-8") == old, f"{p.name} переписан ради даты"


@test
def test_clashes_remember_what_they_have_examined(tmp: Path):
    """`agent:clashes` не разбирает заново то, что уже посмотрел.

    Отметка «соседей смотрели» ставилась после каждой пары и перезаписывалась более
    старой датой: карточка с двумя изменившимися соседями возвращала в очередь первого.
    А группы «об одном предмете» по совпадению текста не отмечались вовсе — каждый
    прогон отправлял их к модели заново и каждый раз не находил противоречий.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp, git=True)
    card(root, "Concepts/Ссылающаяся.md", status="draft", kind="knowledge",
         distilled="2026-09-01", updated="2026-09-01",
         body="По правилам из [[Сосед-1]] и [[Сосед-2]].")
    card(root, "Concepts/Сосед-1.md", status="draft", kind="knowledge",
         distilled="2026-09-04", updated="2026-09-04", body="Правило первое изменилось.")
    card(root, "Concepts/Сосед-2.md", status="draft", kind="knowledge",
         distilled="2026-09-02", updated="2026-09-02", body="Правило второе изменилось.")
    card(root, "Concepts/Срок-А.md", status="draft", kind="knowledge",
         body="Срок подтверждения — пять дней с даты подачи.")
    card(root, "Concepts/Срок-Б.md", status="draft", kind="knowledge",
         body="Срок подтверждения — пять рабочих дней с даты подачи.")
    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False,
           "thinking_roles": {}, "embed": {"model": "m"}}
    calls = []

    def clean(c, role, messages, **kw):
        calls.append(1)
        return {"ok": True, "backend": 1, "model": "m", "log": [],
                "text": json.dumps({"clashes": []})}
    R.clash_groups = lambda cwd, cfg, limit=0: [["Срок-А", "Срок-Б"]]
    cwd = str(root)

    assert len(R.neighbours_behind(cwd)) == 2
    R.run_clashes(cfg, cwd, call=clean)
    assert len(calls) == 3, f"первый прогон: ждали 2 пары и 1 группу, было {len(calls)}"
    stamp = (root / "AuroraKnowledgeDB/Concepts/Ссылающаяся.md").read_text(encoding="utf-8")
    assert 'neighbours: "2026-09-04"' in stamp or "neighbours: 2026-09-04" in stamp, \
        f"отметка не по самой новой дате соседей:\n{stamp[:300]}"
    assert R.neighbours_behind(cwd) == [], "разобранные пары вернулись в очередь"

    calls.clear()
    R.run_clashes(cfg, cwd, call=clean)
    assert calls == [], f"второй прогон снова пошёл к модели: {len(calls)} вызовов"

    p = root / "AuroraKnowledgeDB/Concepts/Срок-Б.md"
    p.write_text(p.read_text(encoding="utf-8").replace("рабочих", "календарных"),
                 encoding="utf-8")
    R.run_clashes(cfg, cwd, call=clean)
    assert len(calls) == 1, "изменился текст группы, а её не посмотрели снова"


@test
def test_the_panel_checks_the_host_before_it_gives_the_token_away(tmp: Path):
    """Главная страница с токеном сессии отдаётся только на петлевой адрес.

    Страница отдавалась раньше проверки `Host`: чужой сайт, чьё имя перенаправили на
    127.0.0.1, читал её и забирал токен. И токен не из ASCII ронял `compare_digest`
    TypeError — клиент получал разрыв соединения вместо отказа.
    """
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path: str, host: str):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        c.putrequest("GET", path, skip_host=True)
        c.putheader("Host", host)
        c.endheaders()
        r = c.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    try:
        code, body = get("/", "evil.example:8787")
        assert code == 403 and ck.TOKEN not in body, "токен отдан на чужое имя хоста"
        code, body = get("/", "localhost:8787")
        assert code == 200 and ck.TOKEN in body, "на свой адрес страница не отдана"
        code, body = get("/", "[::1]:8787")
        assert code == 200, "адрес [::1] с портом не опознан"
        code, body = get("/api/ping?t=%D1%82%D0%BE%D0%BA%D0%B5%D0%BD", "127.0.0.1:8787")
        assert code == 403, f"токен не из ASCII: ждали отказ, получили {code}"
    finally:
        srv.shutdown()


@test
def test_xml_in_utf16_cannot_hide_a_doctype_from_the_docx_reader(tmp: Path):
    """DOCTYPE и ENTITY в UTF-16 не обходят проверку встроенного чтения docx."""
    import zipfile
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    OI = importlib.import_module("office_ingest")
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xml = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">]>'
           f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p>'
           '</w:body></w:document>')
    path = tmp / "u16.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml.encode("utf-16"))
    assert OI.conv_docx_builtin(str(path)) is None, "XML с сущностями в UTF-16 разобран"
    low = tmp / "low.docx"
    with zipfile.ZipFile(low, "w") as z:
        z.writestr("word/document.xml", xml.replace("DOCTYPE", "doctype").encode("utf-8"))
    assert OI.conv_docx_builtin(str(low)) is None, "doctype в нижнем регистре пропущен"


@test
def test_the_embedding_index_is_replaced_whole_or_not_at_all(tmp: Path):
    """Индекс векторов подменяется целиком: сбой посреди записи не оставляет обрубок.

    Файлы писались прямо по месту: MCP-поиск или панель, читавшие индекс в этот момент,
    видели обрезанный файл и отвечали «векторов нет».
    """
    import array
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    E = importlib.import_module("kb_embed")
    root = make_project(tmp)
    old = os.getcwd()
    os.chdir(root)
    try:
        E.META = os.path.join("AuroraKnowledgeDB", "meta")
        E.VECTORS = os.path.join(E.META, "embeddings.bin")
        E.INDEX = os.path.join(E.META, "embeddings.json")
        cards = {"А": {"hash": "x", "row": 0}}
        E.save_index("m", 2, cards, array.array("f", [0.6, 0.8]), None)
        before = (open(E.VECTORS, "rb").read(), open(E.INDEX, encoding="utf-8").read())
        assert not [f for f in os.listdir(E.META) if f.endswith(".tmp")], "остался временный файл"

        class Broken:
            def tofile(self, f):
                f.write(b"half")
                raise OSError("диск кончился")
        try:
            E.save_index("m", 2, cards, Broken(), None)
        except OSError:
            pass
        assert (open(E.VECTORS, "rb").read(), open(E.INDEX, encoding="utf-8").read()) == before, \
            "сбой посреди записи испортил прежний индекс"
    finally:
        os.chdir(old)


@test
def test_a_crash_between_the_two_index_files_is_seen_as_a_stale_index(tmp: Path):
    """Новые вектора при старой карте — индекс чужой, а не «свежий».

    `embeddings.bin` и `embeddings.json` подменяются двумя операциями. Обрыв между ними
    при том же числе карточек и другом порядке оставлял пару, которая проходила сверку
    числа строк: `kb:embed` доверял старым хешам и держал чужие вектора до `--all`.
    """
    import array
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    E = importlib.import_module("kb_embed")
    root = make_project(tmp)
    old = os.getcwd()
    os.chdir(root)
    try:
        E.META = os.path.join("AuroraKnowledgeDB", "meta")
        E.VECTORS = os.path.join(E.META, "embeddings.bin")
        E.INDEX = os.path.join(E.META, "embeddings.json")
        E._FILE_CACHE.clear()
        first = {"А": {"hash": "a", "row": 0}, "Б": {"hash": "b", "row": 1}}
        E.save_index("m", 2, first, array.array("f", [1, 0, 0, 1]), None)
        assert set(E.load_index()["cards"]) == {"А", "Б"}, "целый индекс не читается"
        old_json = open(E.INDEX, encoding="utf-8").read()
        swapped = {"Б": {"hash": "b", "row": 0}, "А": {"hash": "a", "row": 1}}
        E.save_index("m", 2, swapped, array.array("f", [0, 1, 1, 0]), None)
        assert E.load_index()["cards"]["Б"]["row"] == 0, "новый индекс не читается"
        # обрыв после подмены векторов: карта осталась прежней
        open(E.INDEX, "w", encoding="utf-8").write(old_json)
        E._FILE_CACHE.clear()
        assert E.load_index()["cards"] == {}, "пара из разных записей принята за целый индекс"
        # индекс прежнего формата без отпечатка читается как раньше
        legacy = json.loads(old_json)
        legacy.pop("bin")
        open(E.INDEX, "w", encoding="utf-8").write(json.dumps(legacy))
        E._FILE_CACHE.clear()
        assert set(E.load_index()["cards"]) == {"А", "Б"}, "индекс без отпечатка отвергнут"
    finally:
        os.chdir(old)


@test
def test_confluence_config_and_secret_are_read_from_the_given_root(tmp: Path):
    """Конфиг и секрет Confluence читаются из названного корня, а не от рабочей папки процесса.

    Панель разбирала ссылки, переключая `os.chdir` в многопоточном сервере: два запроса
    путали, куда возвращаться, и сервер оставался в чужом проекте.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    C = importlib.import_module("confluence_export")
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Test"\natlassian:\n  confluence:\n'
        '    base_url: "https://wiki.example.org"\n    space: "DOC"\n  jira:\n    project_key: "T"\n',
        encoding="utf-8")
    (root / ".env.aurora.local").write_text("CONFLUENCE_PAT=token-from-root\n", encoding="utf-8")
    before = os.getcwd()
    got = C.read_config(str(root))
    auth, kind = C.read_secret(str(root))
    assert got["base_url"] == "https://wiki.example.org" and got["space"] == "DOC", got
    assert auth == "Bearer token-from-root" and kind == "PAT", (auth, kind)
    assert os.getcwd() == before, "чтение сменило рабочую папку"
    src = (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert "os.chdir(" not in src, "панель снова меняет рабочую папку процесса"


@test
def test_a_stub_gets_its_own_text_back_and_one_status(tmp: Path):
    """Заготовка, чей служебный текст пересказан «тезисом», снова выглядит заготовкой.

    До 1.101 distill брал и заготовки: «Авторизация — заготовка понятия, в которой ссылка
    уже есть…» — пересказ пометки, а сама пометка уехала в дословный раздел (PRJ-C: 345).
    И заготовки в Glossary стояли `draft`, в Concepts — `placeholder`.
    """
    root = make_project(tmp)
    card(root, "Concepts/Авторизация.md",
         "Авторизация — заготовка понятия, в которой ссылка уже есть, а знаний пока нет.\n\n"
         "## Источник (перенесено дословно)\n\n\n# Авторизация\n\n"
         "_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n\n## Упоминается в\n\n"
         "- [[Вход]]\n\n## История изменений\n\n- 2026-08-30: тезис пересобран\n",
         status="draft", tags="[заготовка]", distilled="2026-08-30", relinked="2026-09-01")
    card(root, "Glossary/ТКП.md", "_Заготовка: имя названо в базе._", status="draft",
         tags="[заготовка]")
    run("kb_fix.py", "--stub-text", "--apply", "--allow-dirty", cwd=root)
    kb = root / "AuroraKnowledgeDB"
    a = (kb / "Concepts/Авторизация.md").read_text(encoding="utf-8")
    head, body = a.split("\n---", 1)
    assert "status: placeholder" in head and "distilled:" not in head and "relinked:" not in head, head
    assert "## Источник (перенесено дословно)" not in body and "_Заготовка:" in body, body
    assert "заготовка понятия, в которой" not in body and "## История изменений" in body, body
    assert "status: placeholder" in (kb / "Glossary/ТКП.md").read_text(encoding="utf-8")


@test
def test_a_page_its_authors_retired_is_not_parsed(tmp: Path):
    """Страницу, которую сами авторы вывели из работы, разбор не берёт.

    «[не_используем]ALG-029» (91 КБ), «Вариант… (Устаревший)», «Удалить_или_переиспользовать»,
    прежние версии алгоритмов в «…/Архив/» разбирались на PRJ-C как живые.
    """
    root = make_project(tmp)
    base = root / "Sources/Confluence/Алгоритмы"
    for rel in ("[не_используем]ALG-029._Полный_ФЛК.md", "Архив/ALG-061_Проверка.md",
                "Вариант_(Устаревший)/index.md", "Удаление_документа.md"):
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# Страница\n\n" + "Шаг алгоритма. " * 30, encoding="utf-8")
    plan = run("build_plan.py", cwd=root).stdout
    assert "Удаление_документа" in plan, plan[-400:]
    for gone in ("ALG-029", "ALG-061", "Устаревший"):
        assert gone not in plan, f"отозванная авторами страница в плане: {gone}"


@test
def test_a_live_card_wins_over_its_archived_copy_and_dead_maps_leave(tmp: Path):
    """Ссылка ведёт на живую карточку, а не путается в её копии в архиве.

    Слитая карточка уходит в `_archive` под тем же именем, и «[[ER_Объект_учета_ЮЛ]]»
    находила две — живую и копию — и объявлялась неоднозначной. Там же ссылки на карту
    документа, ушедшую вместе с машинной расшифровкой, держали базу с битыми ссылками.
    """
    root = make_project(tmp)
    card(root, "Concepts/ER-Объект-учета-ЮЛ.md", "Объект учёта.", status="knowledge")
    card(root, "_archive/ER-Объект-учета-ЮЛ.md", "Старая копия.", status="deprecated")
    card(root, "Concepts/Ссылки.md", "См. [[ER_Объект_учета_ЮЛ]].\n\n## Названо в карточках\n\n"
         "- [[Документ--Старая-расшифровка]]\n- [[ER-Объект-учета-ЮЛ]]\n", status="knowledge")
    run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    text = (root / "AuroraKnowledgeDB/Concepts/Ссылки.md").read_text(encoding="utf-8")
    assert "[[ER-Объект-учета-ЮЛ]]" in text and "[[ER_Объект_учета_ЮЛ]]" not in text, text
    assert "Документ--Старая-расшифровка" not in text, f"ссылка на ушедшую карту осталась:\n{text}"


@test
def test_checking_a_pid_never_stops_the_process(tmp: Path):
    """Вопрос «жив ли процесс» не должен его убивать — ни на какой системе.

    `os.kill(pid, 0)` на Windows не проверяет, а завершает процесс. Замок пишущего прогона
    проверял держателя именно им: на windows-latest проверка убила сам тестовый прогон,
    чей pid лежал в замке. Живой дочерний процесс после вопроса обязан остаться живым,
    а завершённый и собранный — стать мёртвым; чужой pid 0 и отрицательные — не жив.
    """
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import pid_alive
    import agent_runner
    assert agent_runner.pid_alive is pid_alive, "у замка и панели должна быть одна проверка"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for _ in range(3):
            assert pid_alive(child.pid), "живой процесс назван мёртвым"
        assert child.poll() is None, "вопрос «жив ли процесс» завершил процесс"
    finally:
        child.terminate()
        child.wait(timeout=30)
    assert not pid_alive(child.pid), "завершённый процесс назван живым"
    assert pid_alive(os.getpid())
    assert not pid_alive(0) and not pid_alive(-5)


@test
def test_windows_pid_check_reads_the_exit_code_not_a_signal(tmp: Path):
    """Ветка Windows сверяется с кодом завершения и считает «нет доступа» признаком жизни.

    На другой системе `kernel32` подставляется: проверяем логику выбора, а не сам вызов.
    STILL_ACTIVE (259) — идёт; любой другой код — закончился; не открылся с ошибкой 5 —
    процесс чужой и жив; не открылся с другой ошибкой — его нет; ручка закрывается всегда.
    """
    sys.path.insert(0, str(SCRIPTS))
    import ctypes
    from aurora_common import _win_pid_alive

    class Kernel32:
        def __init__(self, handle, exit_code=259, ok=1):
            self.handle, self.exit_code, self.ok = handle, exit_code, ok
            self.closed, self.opened = [], []

        def OpenProcess(self, access, inherit, pid):
            self.opened.append((access, inherit, pid))
            return self.handle

        def GetExitCodeProcess(self, handle, ref):
            ref._obj.value = self.exit_code
            return self.ok

        def CloseHandle(self, handle):
            self.closed.append(handle)

    assert ctypes.c_ulong  # ref._obj — сама c_ulong, в которую пишет GetExitCodeProcess
    k = Kernel32(77, 259)
    assert _win_pid_alive(4321, k, lambda: 0) is True and k.closed == [77]
    assert k.opened == [(0x1000, False, 4321)], "права шире «спросить состояние» не нужны"
    k = Kernel32(77, 0)
    assert _win_pid_alive(4321, k, lambda: 0) is False and k.closed == [77]
    k = Kernel32(77, 0, ok=0)
    assert _win_pid_alive(4321, k, lambda: 0) is True, "не смогли спросить — не объявляем мёртвым"
    assert k.closed == [77]
    assert _win_pid_alive(4321, Kernel32(0), lambda: 5) is True, "чужой процесс записан в мёртвые"
    assert _win_pid_alive(4321, Kernel32(0), lambda: 87) is False, "несуществующий pid назван живым"


@test
def test_child_processes_speak_utf8_whatever_the_system_codepage(tmp: Path):
    """Дочерний Python пишет в трубу UTF-8, а не в кодовой странице системы.

    На Windows труба без этого кодируется в cp1251/cp1252: кириллица в ответе адаптера
    превращалась в «?» или роняла запись. Значение, заданное человеком, сильнее умолчания.
    """
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import child_env
    saved = {k: os.environ.pop(k, None) for k in ("PYTHONUTF8", "PYTHONIOENCODING")}
    try:
        env = child_env()
        assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8", env
        assert "PYTHONUTF8" not in os.environ, "умолчание просочилось в окружение самого процесса"
        os.environ["PYTHONIOENCODING"] = "cp866"
        assert child_env()["PYTHONIOENCODING"] == "cp866", "явная настройка человека затёрта"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    for k in ("PYTHONUTF8", "PYTHONIOENCODING"):
        os.environ.pop(k, None)
    try:
        out = subprocess.run([sys.executable, "-c", "print('Проверка — ✓')"], capture_output=True,
                             env=child_env())
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
    assert out.stdout.decode("utf-8").strip() == "Проверка — ✓", out.stdout


@test
def test_replacing_a_busy_file_waits_on_windows_and_fails_at_once_elsewhere(tmp: Path):
    """Подмена файла, который кто-то читает, на Windows ждёт; на другой системе — не ждёт.

    Неделимая запись манифеста, индексов и настроек — это «записать рядом и подменить».
    На Windows подмена файла, открытого другим потоком, падает с `PermissionError`
    (WinError 5 или 32) на ровном месте: на windows-latest так падала параллельная запись
    манифеста. Занятость проходит за миллисекунды, а на POSIX тот же `PermissionError` —
    настоящий отказ в правах, и ждать его бессмысленно.
    """
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import replace_file
    src, dst = tmp / "new.json", tmp / "manifest.json"
    src.write_text("новый", encoding="utf-8")
    dst.write_text("старый", encoding="utf-8")

    real, calls = os.replace, []

    def busy(times):
        def fake(a, b):
            calls.append(1)
            if len(calls) <= times:
                raise PermissionError(5, "Access is denied")
            return real(a, b)
        return fake

    try:
        os.replace = busy(3)
        replace_file(src, dst, windows=True, pause=0.001)
        assert len(calls) == 4, f"ждали три отказа и успех, было вызовов: {len(calls)}"
        assert dst.read_text(encoding="utf-8") == "новый" and not src.exists()

        calls.clear()
        src.write_text("ещё", encoding="utf-8")
        os.replace = busy(99)
        try:
            replace_file(src, dst, windows=False)
        except PermissionError:
            assert len(calls) == 1, "на POSIX отказ в правах повторяют — это ждёт зря"
        else:
            raise AssertionError("отказ в правах проглочен")

        calls.clear()
        try:
            replace_file(src, dst, windows=True, attempts=5, pause=0.001)
        except PermissionError:
            assert len(calls) == 5, f"повторов должно быть ровно {5}, было {len(calls)}"
        else:
            raise AssertionError("вечная занятость не поднята ошибкой")
    finally:
        os.replace = real
    assert dst.read_text(encoding="utf-8") == "новый", "при отказе файл-приёмник испорчен"

    for site in ("build_plan.py", "kb_embed.py", "agent_core.py", "agent_runner.py"):
        code = (SCRIPTS / site).read_text(encoding="utf-8")
        assert "os.replace(" not in code, f"{site}: запись мимо replace_file — на Windows упадёт"


@test
def test_every_script_speaks_utf8_even_into_a_legacy_codepage_pipe(tmp: Path):
    """Скрипт с русским выводом не падает, когда труба в однобайтной кодовой странице.

    На Windows вывод в трубу идёт в cp1251/cp1252, и первая же русская строка — это
    `UnicodeEncodeError`. Пять скриптов (`kit_i18n`, `kb_retrieval`, `sources_registry`,
    `agent_probe`, `dev_qa`) не переключали вывод и роняли тест проверки каталогов
    интерфейса. Здесь кодовая страница трубы подменяется переменной окружения, поэтому
    дефект виден и на Linux: справка каждого скрипта обязана выйти кодом 0 и читаться как
    UTF-8, и обязана это делать вне зависимости от окружения родителя.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONIOENCODING"] = "cp1252"
    bad = []
    checked = 0
    for f in sorted(SCRIPTS.glob("*.py")):
        code = f.read_text(encoding="utf-8")
        if "__main__" not in code or "argparse.ArgumentParser" not in code:
            continue
        cp = subprocess.run([sys.executable, str(f), "--help"], capture_output=True, env=env,
                            timeout=60)
        checked += 1
        err = cp.stderr.decode("utf-8", "replace")
        if cp.returncode != 0 or "UnicodeEncodeError" in err:
            bad.append(f"{f.name}: rc={cp.returncode} {err.strip().splitlines()[-1:]}")
    assert checked > 20, f"справку проверили у {checked} скриптов — перечень не нашёлся"
    assert not bad, "\n".join(bad)


@test
def test_a_null_device_is_not_a_terminal_on_windows(tmp: Path):
    """`stdin=DEVNULL` — не терминал, даже когда `isatty()` отвечает «да».

    На Windows `NUL` — символьное устройство, и `isatty()` для него истинно: `aurora.py new`
    из скрипта, панели или ассистента шёл задавать вопросы в пустоту и падал на первом
    `input()` с `EOFError`. Настоящая консоль отличается тем, что читается её режим.
    """
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import stdin_is_terminal

    class Stream:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    class Kernel32:
        def __init__(self, console):
            self.console = console

        def GetStdHandle(self, which):
            assert which == -10, "спрашивали не стандартный ввод"
            return 7

        def GetConsoleMode(self, handle, ref):
            return 1 if self.console else 0

    assert stdin_is_terminal(Stream(False), Kernel32(True)) is False, "файл или труба названы терминалом"
    assert stdin_is_terminal(Stream(True), Kernel32(False)) is False, "NUL назван терминалом"
    assert stdin_is_terminal(Stream(True), Kernel32(True)) is True, "консоль не узнана"
    assert stdin_is_terminal(None, Kernel32(True)) is False, "нет stdin — это не терминал"
    cp = subprocess.run([sys.executable, "-c",
                         "import sys; sys.path.insert(0, %r); "
                         "from aurora_common import stdin_is_terminal; "
                         "print(stdin_is_terminal())" % str(SCRIPTS)],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    assert cp.stdout.strip() == "False", f"DEVNULL принят за терминал: {cp.stdout!r} {cp.stderr[-200:]}"


@test
def test_a_gitignored_folder_with_a_russian_name_is_recognised(tmp: Path):
    """Папка по-русски, закрытая `.gitignore`, — вне схемы допустима, как и латинская.

    `git check-ignore` печатает не-ASCII пути в кавычках с восьмеричными кодами
    («\\320\\241…»), и доктор не узнавал в них свою папку: рабочая папка с русским именем,
    честно закрытая правилом, считалась нарушением схемы. А на Windows текстовый режим
    подпроцесса превращал `\\n` в `\\r\\n`, и git получал каждый путь с хвостом `\\r`.
    Пути идут байтами и через NUL.
    """
    root = make_project(tmp, git=True)
    (root / "СвояПапка").mkdir()
    (root / "СвояПапка" / "файл.md").write_text("текст", encoding="utf-8")
    (root / "Чужая").mkdir()
    (root / "Чужая" / "файл.md").write_text("текст", encoding="utf-8")
    (root / ".gitignore").write_text("СвояПапка/\n", encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    D = importlib.import_module("aurora_doctor")
    old = os.getcwd()
    os.chdir(root)
    try:
        got = D.git_ignored(["СвояПапка", "Чужая"])
        assert got == {"СвояПапка"}, f"закрытая русская папка не узнана: {got}"
        assert D.git_ignored([]) == set()
    finally:
        os.chdir(old)


@test
def test_a_path_on_another_drive_does_not_stop_the_engine(tmp: Path):
    """Путь на другом диске Windows не роняет разбор: относительного пути между дисками нет.

    `os.path.relpath("C:\\\\x", "D:\\\\y")` — `ValueError: path is on mount 'C:', start on mount
    'D:'`. Карточка, чей путь абсолютный и лежит на другом диске, чем текущая папка, роняла
    разбор раздела (`Card.section`), а синк Confluence падал на подсчёте длины пути. Здесь
    диск «чужой» подменяется функцией, которая бросает ту же ошибку.
    """
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import KB_ROOT, Card, safe_relpath, section_of

    def other_drive(*_a):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    assert safe_relpath("/a/b/c", "/a", relpath=other_drive) == "/a/b/c", "другой диск: нужен абсолютный путь"
    assert safe_relpath("/a/b/c", "/a").replace("\\", "/") == "b/c", "обычный относительный путь изменился"
    assert section_of("C:/Users/me/proj/AuroraKnowledgeDB/Concepts/Заявка.md", relpath=other_drive) \
        == "Concepts"
    assert section_of("C:\\Users\\me\\proj\\AuroraKnowledgeDB\\Glossary\\a.md", relpath=other_drive) \
        == "Glossary"
    assert section_of("C:/elsewhere/a.md", relpath=other_drive) == "", "корня базы нет в пути"
    assert section_of(f"{KB_ROOT}/Systems/АИС.md") == "Systems"
    assert Card("/x/AuroraKnowledgeDB/Roles/Роль.md", "---\ntitle: Роль\n---\n", root="/x/AuroraKnowledgeDB").section \
        == "Roles"

    src = (SCRIPTS / "confluence_export.py").read_text(encoding="utf-8")
    assert "os.path.relpath(os.path.abspath(out), os.getcwd())" not in src, \
        "синк снова считает путь от текущей папки напрямую — на другом диске он упадёт"


def test_the_english_panel_describes_every_command_in_english(tmp: Path):
    """В английском режиме 91 описание команды читается по-английски, а не по-русски.

    Описания приходят от сервера из реестра `commands.txt`, а он один и русский: в
    разделе «Команды», палитре и окне запуска английский экран показывал русский текст у
    каждой команды. Перевод лежит в `cockpit/i18n/data/en.json` — тот же, из которого
    собран английский справочник `docs/en/commands.md`, — и страница сама просит язык.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    reg = [r for r in ck.registry()]
    data = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))
    names = data["commands"]
    ru = {}
    for line in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if line.strip() and not line.startswith("#") and len(parts) == 7:
            ru[parts[1]] = parts[6]
    assert set(names) == set(ru), (sorted(set(ru) - set(names)), sorted(set(names) - set(ru)))
    for cmd, text in names.items():
        outside = re.sub(r"`[^`]*`", "", text)
        assert text.strip() and not re.search("[а-яА-ЯёЁ]", outside), \
            f"{cmd}: описание пусто или осталось по-русски: {text[:80]}"

    got = ck.localized_commands(reg, "en")
    assert len(got) == len(reg) and all(r["what"] == names[r["cmd"]] for r in got if r["cmd"] in names), \
        "описания не заменены на английские"
    assert [r["cmd"] for r in got] == [r["cmd"] for r in reg], "порядок реестра изменился"
    assert got[0]["flags"] == reg[0]["flags"], "перевод тронул не только описание"
    assert ck.localized_commands(reg, "ru") == reg, "русский режим изменил реестр"
    assert ck.localized_commands(reg, "zz") == reg, "неизвестный язык не откатился на русский"
    assert ck.localized_commands(reg, "../../etc/passwd") == reg
    assert ck.request_lang({"lang": ["EN"]}) == "en"
    assert ck.request_lang({"lang": ["../x"]}) == "ru" and ck.request_lang({}) == "ru"

    ui = ui_source()
    assert 'S.lang !== "ru"' in ui and '"&lang=" + encodeURIComponent(S.lang)' in ui, \
        "страница не просит у сервера язык: описания команд придут по-русски"
    assert 'S.state.commands = st.commands' in ui, \
        "смена языка не перечитывает описания команд: они останутся прежними до перезагрузки"

    out = tmp / "commands.md"
    cp = subprocess.run([sys.executable, str(SCRIPTS / "kit_commands.py"), "--en-md", str(out)],
                        cwd=str(KIT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert out.read_text(encoding="utf-8") == (KIT / "docs/en/commands.md").read_text(encoding="utf-8"), \
        "docs/en/commands.md устарел: python3 scripts/kit_commands.py --en-md docs/en/commands.md"

    gap = subprocess.run([sys.executable, str(SCRIPTS / "kit_i18n.py"), "--check"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    assert "данные: commands" in gap.stdout and gap.returncode == 0, gap.stdout[-600:]


@test
def test_the_ask_tab_keeps_the_model_as_data_not_as_russian_text(tmp: Path):
    """Подпись «кто ответил» хранит модель в данных; выгрузка не режет русскую приставку.

    Выгрузка разговора брала модель из текста подписи и отрезала от неё «модель: » —
    по-русски. На английском приставка другая, резать было нечего, и в файл попадало
    «Model: model: X · backend No. 1». Пустая подпись до первого ответа была вшита в
    разметку по-русски и на английском экране оставалась русской.
    """
    node = shutil.which("node")
    if not node:
        return
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    view = ck.module_file("ask", "view.js")
    html = Path(ck.module_file("ask", "view.html")).read_text(encoding="utf-8")
    assert not re.search(r'id="askWho"[^>]*>[^<]*[а-яА-ЯёЁ]', html), \
        "подпись «кто ответил» вшита в разметку по-русски"
    harness = """
import {drawWho, markdown} from "MODULE";
const chip = {dataset: {}, textContent: ""};
const cardsBox = {children: []};
const ctx = {
  t: (k, v) => k + (v ? ":" + JSON.stringify(v) : ""),
  $: q => q === "#askWho" ? chip : q === "#askBody" ? {children: [], querySelectorAll: () => []} : null,
  project: {path: "/p"}, lang: "en",
};
const out = {};
drawWho(ctx); out.empty = chip.textContent;
drawWho(ctx, {kind: "answer", model: "glm-5", n: "2"}); out.answer = chip.textContent; out.model = chip.dataset.model;
ctx.lang = "ru"; drawWho(ctx); out.again = chip.textContent;      // язык сменили — строка собрана заново
drawWho(ctx, {kind: "ping_ok", model: "m1"}); out.ping = chip.textContent;
console.log(JSON.stringify(out));
"""
    src = tmp / "who.mjs"
    src.write_text(harness.replace("MODULE", Path(view).resolve().as_uri()), encoding="utf-8")
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:600]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["empty"] == "ask.who_none", r
    assert r["answer"].startswith("ask.who_line") and '"model":"glm-5"' in r["answer"] \
        and "ask.who_spare" in r["answer"], r
    assert r["model"] == "glm-5" and r["again"] == r["answer"], r
    assert r["ping"] == "ask.primary_answersm1", r
    js = Path(view).read_text(encoding="utf-8")
    assert 'replace(/^модель:' not in js and 'dataset.model' in js, \
        "выгрузка снова режет русскую приставку у подписи"


@test
def test_the_panel_server_answers_in_the_language_the_page_asks_for(tmp: Path):
    """Страница просит язык (`?lang=en`), и сервер отвечает на нём: данные, маршруты, отказы.

    Описания 91 команды, пояснения 295 флагов, маршруты, названия скинов, описания
    коннекторов, находки доктора и сообщения об ошибках рождаются в сервере по-русски. На
    английском экране каждое из них оставалось русским; теперь переводятся по таблице
    `cockpit/i18n/data/en.json` — в одном месте, перед отправкой ответа. Нет перевода — русский
    оригинал, а не пустота.
    """
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    sys.path.insert(0, str(KIT / "cockpit"))
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    # --- движок перевода сообщений
    assert ck.translate_message("проект не выбран", "en") == "no project selected"
    assert ck.translate_message("проект не выбран", "ru") == "проект не выбран"
    assert ck.translate_message("неведомое сообщение", "en") == "неведомое сообщение", \
        "нет перевода — русский оригинал, а не пустота"
    assert ck.translate_message("флаг --x не объявлен командой «kb:lint»", "en") \
        == "the flag --x is not declared by the command «kb:lint»", "подстановки не перенесены"
    nested = ck.translate_message("Маршрут не начат: проект не выбран", "en")
    assert nested == "The route was not started: no project selected", \
        f"причина, вложенная в сообщение, осталась русской: {nested}"
    payload = {"error": "проект не выбран", "name": "проект не выбран",
               "doctor": {"errors": ["нет AGENTS.md"], "warns": ["нет AGENTS.md", 5]},
               "items": [{"why": "путь вне проекта"}]}
    got = ck.translate_payload(payload, "en")
    assert got["error"] == "no project selected" and got["name"] == "проект не выбран", \
        "переведено значение под чужим ключом — данные проекта трогать нельзя"
    assert got["doctor"]["errors"] == ["there is no AGENTS.md"] and got["doctor"]["warns"][1] == 5
    assert got["items"][0]["why"] == "the path is outside the project"
    assert ck.MESSAGE_KEYS == tuple(__import__("kit_i18n").MESSAGE_KEYS), \
        "перечень ключей сообщений в сервере и в проверке каталогов разошёлся"

    # --- настоящий сервер: GET с ?lang=en и POST с отказом
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method: str, path: str, body: str = ""):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
        c.request(method, path + ("&" if "?" in path else "?") + "t=" + ck.TOKEN, body=body or None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8"))
    cyr = re.compile("[а-яА-ЯёЁ]")
    try:
        _code, state = call("GET", "/api/state?lang=en")
        row = next(r for r in state["commands"] if r["cmd"] == "kit:doctor")
        assert row["what"].startswith("Project readiness"), row["what"]
        assert all(not cyr.search(h) for r in state["commands"] for h in r["flag_help"].values()
                   if h.replace("MOC/Сообщества/", "").replace("MOC/Трассировка-требований.md", "")
                   .replace("MOC/Связи.md", "") == h), "пояснение флага осталось русским"
        assert any(i["enables"].startswith("everything:") for i in state["env"]["items"]), \
            "«что даёт» в Установке осталось русским"
        _code, ru_state = call("GET", "/api/state")
        row_ru = next(r for r in ru_state["commands"] if r["cmd"] == "kit:doctor")
        assert cyr.search(row_ru["what"]), "без языка страница получила не русский"

        _code, sc = call("GET", "/api/scenarios?lang=en")
        assert sc["scenarios"][0]["title"] == "Update the base", sc["scenarios"][0]["title"]
        left = [s["why"] for r in sc["scenarios"] for s in r["steps"] if cyr.search(s.get("why", ""))]
        assert not left, f"пояснения шагов остались русскими: {left[:2]}"
        _code, sk = call("GET", "/api/skins?lang=en")
        assert {s["name"] for s in sk["skins"]} >= {"Contrast", "Zine 2.0"}, sk["skins"]

        code, err = call("POST", "/api/run?lang=en",
                         json.dumps({"project": str(tmp / "нет"), "cmd": "kb:lint"}))
        assert code >= 400 and err["error"] == "the project was not found among the discovered ones", err
        _code, ru_err = call("POST", "/api/run", json.dumps({"project": str(tmp / "нет"), "cmd": "kb:lint"}))
        assert ru_err["error"] == "проект не найден среди обнаруженных", ru_err
    finally:
        srv.shutdown()


@test
def test_the_suite_shards_cover_every_check_exactly_once(tmp: Path):
    """Доли набора вместе дают весь набор, и каждая проверка идёт ровно в одной доле.

    На Windows полный прогон идёт ~12 минут, а выпуск ждёт все проверки, поэтому там набор
    режется на три доли (`--shard=i/3`). Доля, потерявшая проверку или взявшая её дважды, —
    либо пропущенный дефект, либо лишнее время.
    """
    sys.path.insert(0, str(KIT / "tests"))
    import harness as H
    whole = [n for n, _f, _i in H.select_tests()]
    for n in (1, 2, 3, 5):
        parts = [[x for x, _f, _i in H.select_tests(shard=(i, n))] for i in range(1, n + 1)]
        flat = [x for p in parts for x in p]
        assert sorted(flat) == sorted(whole), \
            f"{n} долей: потеряно {set(whole) - set(flat)}, лишнее {len(flat) - len(set(flat))}"
        assert max(map(len, parts)) - min(map(len, parts)) <= len(H.INVARIANTS) + 1, \
            f"{n} долей неравны: {[len(p) for p in parts]}"
    for bad in ((0, 3), (4, 3), (1, 0)):
        try:
            H.select_tests(shard=bad)
        except SystemExit:
            continue
        raise AssertionError(f"доля {bad} принята")


@test
def test_script_help_does_not_print_paths_in_the_native_separator(tmp: Path):
    """Пояснения флагов в `--help` называют пути через «/» на любой системе.

    Путь по умолчанию подставлялся в справку прямо из константы, собранной `os.path.join`: на
    Windows в окне запуска панели стояло «AuroraKnowledgeDB\\meta\\graphify\\code.json». А
    английский перевод пояснения ищется по самому тексту, и с «\\» он не находился — на
    Windows английский экран показывал русский текст (`kit_i18n --check` краснел там же).
    """
    import ast
    bad = []
    for f in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "help" or not isinstance(kw.value, ast.JoinedStr):
                    continue
                for part in kw.value.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    expr = ast.unparse(part.value)
                    # число (версия схемы) безопасно; всё остальное — путь, и разделитель должен быть «/»
                    if expr in ("CURRENT",) or "os.sep" in expr:
                        continue
                    bad.append(f"{f.name}:{node.lineno}: {{{expr}}}")
    assert not bad, "в справке путь подставлен как есть:\n" + "\n".join(bad)


@test
def test_no_two_scripts_keep_a_copy_of_the_same_function(_t):
    """Одна и та же функция в двух скриптах не живёт: общее лежит в `aurora_common`.

    `kb_fix` держал свою `git_dirty` и `check_git_guard` рядом с `aurora_common.git_guard`:
    копии отличались одним словом в сообщении и разошлись бы на первой же правке, а отказ
    «грязное дерево» у ремонта и у остальных команд объяснялся бы по-разному.
    """
    import ast as _ast
    import difflib
    by: dict = {}
    for path in sorted(SCRIPTS.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for node in _ast.parse(src).body:
            if isinstance(node, _ast.FunctionDef) and node.name != "main":
                seg = _ast.get_source_segment(src, node) or ""
                # имя параметра и подпись не в счёт: копия с другим именем аргумента — всё та же копия
                body = re.sub(r"^def \w+\([^)]*\)[^:]*:", "", seg)
                if len(body) >= 250:
                    by.setdefault(node.name, []).append((path.name, node.lineno, body))
    twins = []
    for name, defs in by.items():
        for i, a in enumerate(defs):
            for b in defs[i + 1:]:
                if difflib.SequenceMatcher(None, a[2], b[2], autojunk=False).ratio() >= 0.9:
                    twins.append(f"{name}: {a[0]}:{a[1]} ≈ {b[0]}:{b[1]}")
    assert not twins, "копии одной функции в разных скриптах:\n  " + "\n  ".join(twins)


@test
def test_a_live_page_with_retired_words_in_its_title_is_still_parsed(_t):
    """Слово из метки отзыва внутри обычного названия страницу не выводит из разбора.

    «Не используемые поля», «Правила (устаревшие поля)», «Удалить_лишние_пробелы» — живые
    страницы; из плана они пропадали молча, а папка «Не используемые…» уносила всё поддерево.
    """
    sys.path.insert(0, str(SCRIPTS))
    import build_plan as B
    base = "Sources/Confluence/Алгоритмы/"
    for live in ("Не используемые поля.md", "Не используемые в расчёте/index.md",
                 "Правила (устаревшие поля).md", "Статус не используемый.md",
                 "Удалить_лишние_пробелы_в_ФИО.md"):
        assert not B.retired_page(base + live), f"живая страница сочтена отозванной: {live}"
    for gone in ("[не_используем]ALG-029.md", "Вариант_(Устаревший)/index.md",
                 "ALG-1 (не используем).md", "Удалить_или_переиспользовать_ALG-145.md",
                 "Архив/ALG-061.md", "Архив.md"):
        assert B.retired_page(base + gone), f"отозванная страница в разборе: {gone}"


@test
def test_a_stub_without_a_status_line_gets_one_once(tmp: Path):
    """Заготовка, у которой строки `status:` нет совсем, получает её — и один раз.

    Подмена искала строку, не находила, а отчёт всё равно писал «статус поставлен»:
    файл не менялся, и каждый следующий прогон делал вид, что чинит то же самое.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    stub = kb / "Concepts/БезСтатуса.md"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text('---\ntitle: "БезСтатуса"\ntype: concept\ntags: [заготовка]\n---\n\n'
                    "# БезСтатуса\n\n_Заготовка: имя названо._\n", encoding="utf-8")
    first = run("kb_fix.py", "--stub-text", "--apply", "--allow-dirty", cwd=root).stdout
    text = stub.read_text(encoding="utf-8")
    assert "status: placeholder" in text, text
    assert "поставлен у 1" in first, first
    second = run("kb_fix.py", "--stub-text", "--apply", "--allow-dirty", cwd=root).stdout
    assert "поставлен у 0" in second and stub.read_text(encoding="utf-8") == text, second


@test
def test_project_launcher_and_git_hooks_do_not_trust_python3(tmp: Path):
    """Пусковой файл в проекте и хуки git ищут Python запуском, а `.bat` пишется с CRLF.

    На Windows нет `python3` (есть `python` и `py`), а `python3` из WindowsApps —
    заглушка магазина. Пуш-хук звал `python3` напрямую и на такой машине останавливал
    любой пуш; `.bat` в проекте искал Python через `where` и принимал заглушку; с одними
    LF (установка с macOS или из WSL) cmd.exe промахивается мимо меток `goto`.
    """
    tpl = (KIT / "templates/launchers/start-aurora.bat").read_text(encoding="utf-8")
    assert ":find_py" in tpl and "sys.version_info >= (3, 9)" in tpl and "where py" not in tpl, \
        "шаблон .bat проекта снова доверяет поиску по PATH"
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    inst = importlib.import_module("install_aurora")
    target = tmp / "proj"
    target.mkdir()
    ins = inst.Installer(target, "Demo", None, None, None, True, False)
    ins.install_launchers()
    bat = (target / "start-aurora.bat").read_bytes()
    assert bat.count(b"\r\n") == bat.count(b"\n") > 5, "установленный .bat без CRLF"
    sys.path.insert(0, str(KIT / "scripts"))
    H = importlib.import_module("aurora_hooks")
    for name in ("HOOK", "PUSH_HOOK", "MSG_HOOK"):
        body = getattr(H, name)
        assert "python3 \"$LINT\"" not in body and "| python3 " not in body, \
            f"хук {name} зовёт python3 напрямую"
        assert "'py -3'" in body, f"хук {name} не пробует py -3"
    sh = shutil.which("sh")
    if not sh or os.name == "nt":
        return
    fake = tmp / "bin"
    fake.mkdir()
    (fake / "python3").write_text("#!/bin/sh\necho 'Python was not found' >&2\nexit 9\n", encoding="utf-8")
    (fake / "python3").chmod(0o755)
    (fake / "python").symlink_to(sys.executable)
    repo = tmp / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/aurora_hooks.py").write_text(
        "import sys\nsys.stdin.read()\nprint('scanned')\n", encoding="utf-8")
    hook = repo / "pre-push"
    hook.write_text(H.PUSH_HOOK.format(marker=H.PUSH_MARKER,
                                       branches=" ".join(H.PRIVATE_BRANCHES)), encoding="utf-8")
    cp = subprocess.run([sh, str(hook), "origin", "https://github.com/x/y.git"], cwd=str(repo),
                        input="", capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=30, env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp)})
    assert cp.returncode == 0 and "scanned" in cp.stdout, \
        f"пуш-хук не нашёл python за заглушкой python3: rc={cp.returncode}\n{cp.stdout}{cp.stderr}"


@test
def test_a_later_repair_step_keeps_what_an_earlier_one_wrote(tmp: Path):
    """Шаг ремонта берёт карточку из плана, а не с диска: правка прежнего шага не теряется.

    `--links` исправлял ссылку, затем `--copies` брал `c.text` с диска и писал карточку без
    этой правки: отчёт говорил «исправлено», а в файле оставалось прежнее. То же у
    `--terms` и `--template`.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    d = root / "Raw/customer/FAQ"
    d.mkdir(parents=True)
    human = d / "Памятка_для_перевозчиков.md"
    human.write_text("# Памятка\n\n> **Источник:** `Памятка для перевозчиков.pdf`\n\n"
                     + "Перевозчик предъявляет QR-код. " * 20, encoding="utf-8")
    machine = d / "Памятка для перевозчиков.md"
    machine.write_text('---\ntitle: "П"\nconverted_from: "Raw/customer/FAQ/Памятка для '
                       'перевозчиков.pdf"\n---\n\n' + "Перевозчик код. " * 20, encoding="utf-8")
    rel = lambda p: p.relative_to(root).as_posix()  # noqa: E731
    card(root, "Processes/Предъявление-кода.md",
         f"Перевозчик. См. [[жив-карточка]].\n\n## Источник (перенесено дословно)\n\n"
         f"### {rel(human)}\n\nИз копии.\n\n### {rel(machine)}\n\nИз расшифровки.\n",
         status="knowledge", kind="knowledge", distilled="2026-09-20",
         sources=f'\n  - "{rel(human)}"\n  - "{rel(machine)}"')
    card(root, "Concepts/Жив-карточка.md", "x", status="knowledge")
    man = kb / "meta/manifest.json"
    man.parent.mkdir(parents=True, exist_ok=True)
    man.write_text(json.dumps({"sources": {rel(human): {"cards": 1}, rel(machine): {"cards": 2}}}),
                   encoding="utf-8")
    run("kb_fix.py", "--links", "--copies", "--apply", "--allow-dirty", cwd=root)
    text = (kb / "Processes/Предъявление-кода.md").read_text(encoding="utf-8")
    assert "[[Жив-карточка]]" in text and "[[жив-карточка]]" not in text, text
    assert rel(machine) not in text.split("## Источник", 1)[0], "копия не снята"


@test
def test_the_readonly_guard_sees_the_path_the_way_the_filesystem_does(tmp: Path):
    """`./Sources/x.md` и `Artifacts/../Sources/x.md` — те же источники, и запись в них закрыта.

    Охрана сверяла приставку с сырой строкой браузера, а путь нормализовался уже после неё:
    так через `./` писали в зеркала и удаляли карточки базы знаний.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("cockpit_ro", KIT / "cockpit" / "aurora_cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(mod)
    root = make_project(tmp)
    src = root / "Sources/Confluence/x.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("зеркало", encoding="utf-8")
    kb_card = card(root, "Concepts/Карточка.md", "тело", status="knowledge")
    for rel in ("Sources/Confluence/x.md", "./Sources/Confluence/x.md",
                "Artifacts/../Sources/Confluence/x.md", "/Sources/Confluence/x.md",
                "sources/Confluence/x.md"):
        out = mod.file_write(str(root), rel, "подмена")
        assert "только для чтения" in out.get("error", ""), (rel, out)
    assert src.read_text(encoding="utf-8") == "зеркало", "источник перезаписан в обход охраны"
    for rel in ("./AuroraKnowledgeDB/Concepts/Карточка.md",
                "Artifacts/../AuroraKnowledgeDB/Concepts/Карточка.md"):
        out = mod.file_delete(str(root), rel)
        assert out.get("error"), (rel, out)
    assert kb_card.is_file(), "карточка базы знаний удалена в обход охраны"
    assert mod.why_readonly("AuroraKnowledgeDB/Decisions/./DR-1.md") == ""


@test
def test_the_dashboard_server_serves_only_the_report_and_refuses_strangers(tmp: Path):
    """Сервер дашборда раздаёт папку отчёта, а не весь проект, и не слушает чужие страницы.

    Раздавался корень проекта: `/.env.aurora.local` с токенами, `/.git/config` и база знаний
    отдавались по HTTP без проверки, а `GET /__rebuild` запускал пять скриптов от имени любой
    страницы в браузере.
    """
    import socket
    import time
    import urllib.error
    import urllib.request
    root = make_project(tmp)
    (root / ".env.aurora.local").write_text("JIRA_PERSONAL_TOKEN=SECRET123\n", encoding="utf-8")
    rep = root / "Artifacts/reports"
    rep.mkdir(parents=True, exist_ok=True)
    (rep / "page.html").write_text("<html>отчёт</html>", encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(KIT / "reports/analyst/serve_dashboard.py"),
                             "--port", str(port)], cwd=str(root),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def get(path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        assert get("/Artifacts/reports/page.html")[0] == 200, "отчёт не отдаётся"
        code, body = get("/.env.aurora.local")
        assert code == 404 and "SECRET123" not in body, (code, body)
        assert get("/aurora.config.yaml")[0] == 404, "конфиг проекта отдаётся"
        assert get("/Artifacts/reports/../../.env.aurora.local")[0] == 404
        assert get("/Artifacts/reports/page.html", {"Host": "evil.com"})[0] == 403, \
            "ответил на чужое имя хоста"
        code, _ = get("/__rebuild", {"Sec-Fetch-Site": "cross-site"})
        assert code == 403, "пересборку запустила чужая страница"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@test
def test_the_panel_forgets_old_finished_jobs(_t):
    """Законченные задания не копятся в памяти панели вечно, идущие остаются.

    Каждое задание держит до четырёх тысяч строк вывода и сам процесс; нигде не снимались.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("cockpit_jobs", KIT / "cockpit" / "aurora_cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(mod)
    mod.JOBS.clear()
    for n in range(60):
        mod.JOBS[f"d{n}"] = {"id": f"d{n}", "done": True, "finished": n, "out": ["x"] * 5}
    mod.JOBS["live"] = {"id": "live", "done": False, "out": []}
    with mod.JOBS_LOCK:
        gone = mod.trim_jobs(keep=10)
    assert gone == 50 and len(mod.JOBS) == 11, (gone, len(mod.JOBS))
    assert "live" in mod.JOBS, "идущее задание забыто"
    assert "d59" in mod.JOBS and "d0" not in mod.JOBS, "забыты не самые старые"
    mod.JOBS.clear()


@test
def test_a_multiline_question_keeps_the_thread_header_intact(tmp: Path):
    """Вопрос в несколько строк не ломает шапку журнала разговора.

    Заголовок брался срезом вопроса как есть: перевод строки внутри кавычек шапки обрывал
    `title:` на первой строке, а остальные строки вопроса уходили в заголовок `#` мимо него.
    """
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R
    from aurora_common import frontmatter
    path = tmp / "t.md"
    R.append_turn(path, 'Как работает\nдоверие "карточек"?\nВторая \\ строка', "ответ", "н", "ask")
    text = path.read_text(encoding="utf-8")
    head = text.split("\n---\n", 1)[0]
    assert head.count("\n") == 4, f"шапка разъехалась на лишние строки:\n{head}"
    assert frontmatter(text)["title"] == "Как работает доверие карточек? Вторая / строка", \
        frontmatter(text)
    assert "# Разговор с базой — Как работает доверие карточек? Вторая / строка\n" in text
    assert R.read_thread(path)[0]["q"].count("\n") == 2, "сам вопрос должен остаться как был"


@test
def test_dead_map_links_leave_the_last_line_and_keep_crlf(tmp: Path):
    """Ссылка на ушедшую карту документа уходит и с последней строки файла без `\\n`.

    Строка без перевода строки в конце не находилась, и ссылку превращали в слова, оставляя
    в заготовке «- Документ--Нет». А снятие ссылок читало карточки с пересборкой переводов
    строк: файл с CRLF целиком становился LF, хотя менялась одна строка.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    stub = card(root, "Concepts/Заг.md", "_Заготовка: имя названо._\n\n## Упоминается в\n\n"
                "- [[Документ--Нет]]", status="placeholder", tags="[заготовка]")
    stub.write_text(stub.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    assert "Документ--Нет" not in stub.read_text(encoding="utf-8"), stub.read_text(encoding="utf-8")
    sys.path.insert(0, str(SCRIPTS))
    import kb_moc
    crlf = kb / "Concepts/Crlf.md"
    crlf.write_bytes("---\r\ntitle: Crlf\r\n---\r\n\r\nСм. [[Документ--Док]] и текст.\r\n\r\n"
                     "- [[Документ--Док]]\r\n- [[Жив]]\r\n".encode("utf-8"))
    cwd = os.getcwd()
    os.chdir(root)
    try:
        assert kb_moc.drop_links_to({"Документ--Док"}) == 1
    finally:
        os.chdir(cwd)
    data = crlf.read_bytes()
    assert b"\r\n" in data and data.count(b"\n") == data.count(b"\r\n"), data
    assert "См. Док и текст." in data.decode("utf-8") and b"[[\xd0\x96\xd0\xb8\xd0\xb2]]" in data


@test
def test_two_documents_with_one_file_name_get_two_maps(tmp: Path):
    """Документы с одинаковым именем файла из разных папок не пишут одну карту.

    Вторая карта затирала первую: на каждом прогоне «записано 2, без изменений 0», а знание
    первого документа в картах не оставалось.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    for d, n in (("A", "Один"), ("A", "Два"), ("B", "Три"), ("B", "Четыре")):
        card(root, f"Concepts/{n}.md", f"Знание {n}. " * 5, status="knowledge",
             sources=f'\n  - "Sources/Confluence/{d}/Док.md"')
    (root / "moc_groups.txt").write_text("Концепты: Concepts\n", encoding="utf-8")
    run("kb_moc.py", "--by-source", "--apply", "--allow-dirty", cwd=root)
    maps = sorted(p.name for p in (kb / "MOC").glob("Документ--*.md"))
    assert maps == ["Документ--A-Док.md", "Документ--B-Док.md"], maps
    second = run("kb_moc.py", "--by-source", "--apply", "--allow-dirty", cwd=root).stdout
    assert "без изменений: 2" in second, second[-300:]
    a, b = [(kb / "MOC" / m).read_text(encoding="utf-8") for m in maps]
    assert "[[Один|" in a and "[[Три|" not in a and "[[Три|" in b, (a, b)


@test
def test_a_garbage_model_reply_is_not_a_verdict_on_twins_and_translit(tmp: Path):
    """Ответ «извините, не могу» не становится вердиктом «разные сущности» или «не транслит».

    Нераспознанный ответ превращался в пустой словарь, а пустой словарь читался как «не
    сливать» (карточки получали раздел «Не путать», и группу больше не спрашивали) и как
    «переводить нечего» (имя навсегда уходило из словаря переводов).
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    same = "Профиль обслуживания абонента задаёт перечень доступных услуг в биллинге. " * 4
    card(root, "Concepts/Профиль-абонента.md", status="knowledge", kind="knowledge", body=same)
    card(root, "Concepts/Что-такое-профиль.md", status="draft", kind="knowledge", body=same)
    card(root, "Concepts/Profil-abonenta.md", status="draft", kind="knowledge", body=same)
    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False,
           "thinking_roles": {}, "embed": {"model": "m"}}

    def garbage(c, role, messages, **kw):
        return {"ok": True, "backend": 1, "model": "m", "log": [],
                "text": "извините, не могу ответить"}

    step = R.solve_twins(cfg, str(root), ["Профиль-абонента", "Что-такое-профиль"],
                         apply=True, call=garbage)
    assert step["status"] == "сбой" and not step.get("apart"), step
    text = (root / "AuroraKnowledgeDB/Concepts/Профиль-абонента.md").read_text(encoding="utf-8")
    assert "Не путать" not in text, "мусорный ответ записан как решение «разные»"
    one = R.solve_translit(cfg, str(root / "AuroraKnowledgeDB/Concepts/Profil-abonenta.md"),
                           call=garbage)
    assert one["status"] == "сбой", one
    empty = lambda c, role, messages, **kw: {"ok": True, "backend": 1, "model": "m",  # noqa: E731
                                              "log": [], "text": '{"cyrillic": ""}'}
    assert R.solve_translit(cfg, str(root / "AuroraKnowledgeDB/Concepts/Profil-abonenta.md"),
                            call=empty)["status"] == "не транслит"


@test
def test_a_clash_quote_must_be_found_in_the_cards_and_junk_items_do_not_crash(tmp: Path):
    """Цитата спора обязана найтись в показанном модели тексте карточек; мусор не роняет прогон.

    Код проверял только длину цитаты, хотя комментарий обещал «не нашли в карточке — не
    берём»: выдуманные цитаты становились «спором». А ответ вида {"clashes": ["oops"]}
    падал `AttributeError` на всём прогоне, теряя отметки уже разобранных групп.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    R = importlib.import_module("agent_runner")
    root = make_project(tmp)
    card(root, "Concepts/Срок-А.md", status="draft", kind="knowledge",
         body="Срок подтверждения — пять дней с даты подачи.")
    card(root, "Concepts/Срок-Б.md", status="draft", kind="knowledge",
         body="Срок подтверждения — три дня с даты подачи.")
    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False,
           "thinking_roles": {}, "embed": {"model": "m"}}

    def reply(payload):
        return lambda c, role, messages, **kw: {"ok": True, "backend": 1, "model": "m",
                                                "log": [], "text": json.dumps(
                                                    payload, ensure_ascii=False)}
    group = ["Срок-А", "Срок-Б"]
    made_up = R.solve_clash(cfg, str(root), group, call=reply({"clashes": [
        {"cards": group, "about": "срок", "a": "полностью выдуманная цитата один",
         "b": "полностью выдуманная цитата два"}]}))
    assert made_up["clashes"] == [] and made_up["status"] == "чисто", made_up
    spaced = R.solve_clash(cfg, str(root), group, call=reply({"clashes": [
        {"cards": group, "about": "срок", "a": "Срок подтверждения —  пять дней\nс даты подачи.",
         "b": "Срок подтверждения — три дня с даты подачи."}]}))
    assert len(spaced["clashes"]) == 1, spaced
    for junk in ({"clashes": ["oops"]}, {"clashes": [{"cards": "Срок-А"}]}):
        assert R.solve_clash(cfg, str(root), group, call=reply(junk))["clashes"] == []
    assert R.solve_clash(cfg, str(root), group, call=reply({"clashes": "none"}))["status"] == "сбой"
