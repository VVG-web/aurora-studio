"""Проверки движка Aurora, часть 28: разбор промпта бота с ИИ (1.164.0).

Каркас и помощники — tests/harness.py. Бот работает без человека, и промпт, написанный «как
для человека», ломался тихо: модель искала скрипт, которого нет, ждала переменную окружения,
ходила прямым HTTP — узнавалось это после прогона. Кнопка «Улучшить с ИИ» показывает это до
прогона: движок собирает факты (настоящие инструменты бота и признаки того, чего у него нет),
модель разбирает промпт и по просьбе человека возвращает новый — разговор идёт дальше уже о
нём.

Там же — расписание одной панели: вторая панель машины не продолжает чужую идущую цепочку и
не запускает задания.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import time

from harness import KIT, SCRIPTS, make_project, test  # noqa: F401

COCKPIT = KIT / "cockpit"

BODY = """# Ревью истории

1. Возьми время: `date`, сохрани в `$STATE_DIR/last.json` (export STATE_DIR=/tmp/x).
2. Если есть lockfile — выйди.
3. Запусти `./next.sh`, затем GET /rest/api/2/search?jql=project=ABC.
4. Проверь историю US-3.6.21 и задачу ABC-46.
5. Сохрани отчёт через save_output.
"""

SERVERS = {
    "aurora": {"command": sys.executable, "args": [str(SCRIPTS / "aurora_mcp.py"), "/p"]},
    "tracker": {"command": "tracker-mcp", "about": "задачи трекера"},
    "unused": {"command": "unused-mcp", "about": "не упомянут в промпте"},
    "wiki": {"command": "wiki-mcp", "about": "страницы вики"},
}


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _probe(calls):
    def probe(project, servers=None, describe=False, timeout=60):
        calls.append((sorted(servers or {}), describe))
        out = {}
        for name in servers or {}:
            out[name] = ({"ok": True, "list": [
                {"name": "issue_get", "description": "задача\n  по ключу", "schema": {
                    "type": "object", "required": ["key"],
                    "properties": {"key": {"type": "string", "description": "ключ задачи"},
                                   "fields": {"type": "array", "items": {"type": "string"}},
                                   "mode": {"type": "string", "enum": ["short", "full"]}}}},
                {"name": "issue_find", "description": "поиск задач"}]}
                         if name == "tracker" else {"ok": False, "error": "нет команды"})
        return {"ok": True, "servers": out}
    return probe


@test
def test_the_coach_sees_what_the_prompt_relies_on_but_the_bot_lacks(_t):
    """Признаки того, чего у бота нет: переменные окружения, скрипты и командная строка,
    прямые REST-запросы, замки; серверы бота, которые промпт не упоминает. Коды историй
    (US_…) — не переменные окружения."""
    _path()
    import bot_coach as BCo
    inv = {"selected": {"aurora": {"ok": True, "tools": [{"name": "time_now"}]},
                        "tracker": {"ok": True, "tools": [{"name": "issue_get"}]},
                        "unused": {"ok": True, "tools": [{"name": "noop"}]}}}
    fl = BCo.flags(BODY + "\nВызови tracker_issue_get. US_LOGIN_FLOW\n", inv)
    assert fl["env"] == ["STATE_DIR"], fl["env"]
    assert "next.sh" in fl["shell"] and "export STATE_DIR=" in fl["shell"], fl["shell"]
    assert any(x.startswith("/rest/api/2/search") for x in fl["rest"]), fl["rest"]
    assert fl["locks"] is True
    assert set(fl["unused_servers"]) == {"aurora", "unused"}, fl["unused_servers"]
    assert fl["mentioned_tools"]["tracker"] == ["issue_get"], fl["mentioned_tools"]
    clean = BCo.flags("Найди задачу инструментом `time_now`.", inv)
    assert not (clean["env"] or clean["shell"] or clean["rest"] or clean["locks"]), clean


@test
def test_the_coach_lists_the_bot_s_real_tools(tmp: Path):
    """Инструменты бота на самом деле: встроенные всегда; сервер Авроры — по его списку без
    запуска; другие серверы бота спрашиваются у самих серверов (с описаниями), а уже
    спрошенные не спрашиваются снова; неотвечающий — с причиной; невыбранные — в доступных."""
    _path()
    import agent_core as AG
    import aurora_mcp as M
    import bot_coach as BCo
    root = make_project(tmp)
    saved = AG.mcp_config
    AG.mcp_config = lambda project, kit=None, with_aurora=False, aurora_root="": {"mcpServers": SERVERS}
    calls = []
    try:
        inv = BCo.inventory(root, {"mcp": ["aurora", "tracker", "wiki", "нет-такого"]}, probe=_probe(calls))
        assert calls == [(["tracker", "wiki"], True)], calls
        assert [t["name"] for t in inv["selected"]["aurora"]["tools"]] == [t["name"] for t in M.TOOLS]
        assert inv["selected"]["tracker"] == {"ok": True, "tools": [
            {"name": "issue_get", "description": "задача по ключу",
             "params": "key*: string (ключ задачи); fields: [string]; mode: short|full"},
            {"name": "issue_find", "description": "поиск задач", "params": ""}]}, inv["selected"]["tracker"]
        review = next(t for t in inv["selected"]["aurora"]["tools"] if t["name"] == "jira_publish")
        assert "issue*: string" in review["params"] and "dry_run: boolean" in review["params"], review
        assert inv["selected"]["wiki"]["ok"] is False and "нет команды" in inv["selected"]["wiki"]["error"]
        assert inv["available"] == {"unused": "не упомянут в промпте"}, inv["available"]
        assert {t["name"] for t in inv["builtin"]} >= {"kb_search", "save_output"}
        again = BCo.inventory(root, {"mcp": ["aurora", "tracker", "wiki"]}, probe=_probe(calls),
                              cached=inv)
        assert len(calls) == 1, "уже спрошенный сервер спрошен снова"
        assert again["selected"]["tracker"] == inv["selected"]["tracker"]
    finally:
        AG.mcp_config = saved


@test
def test_the_coach_reply_carries_a_new_prompt_and_server_hints(_t):
    """Ответ модели: новый промпт — между `<<<PROMPT` и `PROMPT>>>`, подсказка о серверах —
    строкой META; в тексте разговора блоков нет. Битый META — не подсказка."""
    _path()
    import bot_coach as BCo
    text = ("Убрал скрипты.\n<<<PROMPT\n# Ревью\n\n1. Время — `aurora_time_now`.\nPROMPT>>>\n"
            '<<<META {"mcp_add": ["aurora"], "mcp_remove": ["unused", ""]} META>>>\nГотово.')
    got = BCo.parse_reply(text)
    assert got["prompt"] == "# Ревью\n\n1. Время — `aurora_time_now`.", got["prompt"]
    assert got["meta"] == {"mcp_add": ["aurora"], "mcp_remove": ["unused"]}, got["meta"]
    assert "<<<" not in got["text"] and "Убрал скрипты." in got["text"] and "Готово." in got["text"]
    assert BCo.parse_reply("Просто разбор.") == {"text": "Просто разбор.", "prompt": None, "meta": None}
    assert BCo.parse_reply("x <<<META {битый} META>>>")["meta"] is None


@test
def test_a_coach_conversation_goes_on_with_the_changed_prompt(tmp: Path):
    """Разговор: пустое первое сообщение — первый разбор по разделам; факты движка и текущий
    промпт — в задании модели; новый промпт возвращается панели; следующая реплика видит
    историю и уже изменённый промпт; своя модель — ровно она; «начать заново» забывает всё."""
    _path()
    import agent_core as AG
    import bot_coach as BCo
    import bots as B
    root = make_project(tmp)
    r = B.create(root, "Ревью", {"mcp": ["aurora", "tracker"]}, BODY)
    rel = r["file"]
    meta = B.read(root, rel)["meta"]
    seen = []
    saved = (AG.mcp_config, AG.config, AG.pin_model)
    AG.mcp_config = lambda project, kit=None, with_aurora=False, aurora_root="": {"mcpServers": SERVERS}
    AG.config = lambda: {"budget_min": 7, "request_timeout": 5}
    AG.pin_model = lambda cfg, spec, roles: dict(cfg, pinned=(spec, tuple(roles)))

    def call(cfg, role, messages, **kw):
        seen.append({"cfg": cfg, "role": role, "messages": messages})
        if len(seen) == 1:
            return {"ok": True, "model": "m1", "text": "**Не выполнится**: `./next.sh` — скриптов нет."}
        if len(seen) == 2:
            return {"ok": True, "model": "m1", "text": "Переписал.\n<<<PROMPT\n# Ревью\n\n1. Время — "
                    "`aurora_time_now`.\nPROMPT>>>\n<<<META {\"mcp_remove\": [\"tracker\"]} META>>>"}
        return {"ok": False, "log": ["шлюз не отвечает"]}
    try:
        first = BCo.turn(root, rel, meta, BODY, call=call, probe=_probe([]))
        assert first["ok"] and first["prompt"] is None, first
        sys_text = seen[0]["messages"][0]["content"]
        assert seen[0]["role"] == BCo.COACH_ROLE
        assert seen[0]["messages"][-1]["content"] == BCo.FIRST
        for need in ("`issue_get`(key*: string (ключ задачи)", "`review_page`(page*: string", "STATE_DIR",
                     "next.sh", "/rest/api/2/search", "Срок прогона: 7 мин", "<<<CURRENT", "Возьми время",
                     "только с `dry_run: true`"):
            assert need in sys_text, f"в задании модели нет «{need}»"
        assert first["flags"]["env"] == ["STATE_DIR"]

        second = BCo.turn(root, rel, meta, BODY, "Перепиши под инструменты", pick="model:prov/big",
                          call=call, probe=_probe([]))
        assert second["prompt"] == "# Ревью\n\n1. Время — `aurora_time_now`.", second
        assert second["meta"] == {"mcp_add": [], "mcp_remove": ["tracker"]}, second["meta"]
        assert seen[1]["cfg"]["pinned"] == ("prov/big", (BCo.COACH_ROLE,)), "своя модель не выбрана"
        assert [m["role"] for m in seen[1]["messages"]] == ["system", "user", "assistant", "user"]
        assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user", "assistant"]
        assert second["messages"][-1]["prompt_changed"] is True
        assert isinstance(second["messages"][-1]["seconds"], int), "реплика без времени ответа"
        assert "<<<PROMPT" not in second["messages"][-1]["content"]
        assert second["messages"][-1]["prompt"] == second["prompt"], "промпт ответа не сохранён"

        try:
            BCo.turn(root, rel, meta, second["prompt"], "А риски?", call=call, probe=_probe([]))
            raise AssertionError("молчание модели прошло как ответ")
        except BCo.CoachError as e:
            assert "шлюз не отвечает" in str(e)
        assert "aurora_time_now" in seen[2]["messages"][0]["content"], "разговор не видит изменённый промпт"
        assert "Возьми время" not in seen[2]["messages"][0]["content"]
        assert len(BCo.load(root, rel)["messages"]) == 4, "неудачная реплика попала в историю"
        try:
            BCo.turn(root, rel, meta, BODY, "  ", call=call)
            raise AssertionError("пустая реплика в начатом разговоре ушла модели")
        except BCo.CoachError:
            pass
        assert BCo.reset(root, rel)["ok"] and BCo.load(root, rel) == {}
        assert not list((root / ".aurora/state/bots/coach").glob("*.tmp")), "брошен временный файл"
    finally:
        AG.mcp_config, AG.config, AG.pin_model = saved


@test
def test_the_panel_talks_only_about_bots_of_the_project(tmp: Path):
    """Панель: разговор — только о файле из bots/ проекта (путь приходит из браузера);
    «Начать заново» и пустой разговор отвечают без модели. Окно раздела зовёт именно эти
    пути, а надписи кнопки и окна есть в обоих каталогах строк."""
    _path()
    import aurora_cockpit as ck
    import bots as B
    root = make_project(tmp)
    rel = B.create(root, "Ревью", {}, BODY)["file"]
    for bad in ("../aurora.config.yaml", "bots/../x.md", "bots/нет.md", "Raw/x.md", ""):
        assert ck.coach_state(str(root), bad).get("error"), bad
        assert ck.coach_action(str(root), {"file": bad, "reset": True}, "ru")["ok"] is False, bad
    assert ck.coach_state(str(root), rel) == {"messages": [], "busy": False, "busy_since": None,
                                              "inventory_at": None}
    assert ck.coach_action(str(root), {"file": rel, "reset": True}, "ru") == {"ok": True}
    # Реплика идёт минутами: вторая к тому же боту получает отказ, окно ждёт первую.
    key = ck._coach_key(str(root), rel)
    ck.COACH_BUSY[key] = 1.0
    try:
        assert ck.coach_state(str(root), rel)["busy"] is True
        got = ck.coach_action(str(root), {"file": rel, "message": "ещё"}, "ru")
        assert got["ok"] is False and got["busy"] is True, got
    finally:
        ck.COACH_BUSY.pop(key, None)
    import bot_coach as BCo
    saved = BCo.turn

    def fails(*a, **k):
        assert key in ck.COACH_BUSY, "реплика идёт без отметки «идёт»"
        raise BCo.CoachError("модель не ответила")
    BCo.turn = fails
    try:
        got = ck.coach_action(str(root), {"file": rel, "message": "ещё", "meta": {}}, "ru")
    finally:
        BCo.turn = saved
    assert got == {"ok": False, "error": "модель не ответила"}, got
    assert key not in ck.COACH_BUSY, "отметка «идёт» осталась после реплики"
    view = (COCKPIT / "modules/bots/view.js").read_text(encoding="utf-8")
    assert '"/api/bots/coach?' in view and 'post(ctx, "/api/bots/coach"' in view
    keys = {k for k in re.findall(r'"(bots\.coach\.[\w.]+)"', view) if not k.endswith(".")}
    keys |= {f"bots.coach.{p}.{k}" for p in ("q", "qt") for k in ("rewrite", "trim", "missing", "risks")}
    import json
    for lang in ("ru", "en"):
        have = json.loads((COCKPIT / f"modules/bots/i18n/{lang}.json").read_text(encoding="utf-8"))
        missing = sorted(k for k in keys if k not in have)
        assert not missing, f"{lang}: нет строк {missing}"


@test
def test_a_second_panel_neither_resumes_nor_runs_the_schedule(tmp: Path):
    """Расписание и цепочки ведёт одна панель машины: история и задания лежат в `~/.aurora`,
    общие для всех панелей. 07.10.2026 вторая панель (копия кита на другом порту) при старте
    сочла идущую цепочку брошенной и продолжила её параллельно — чужой `agent:distill` занял
    замок базы, и маршрут живой цепочки встал на `agent:twins`.

    Теперь вторая панель — наблюдатель: не продолжает чужую цепочку, не запускает задания и
    говорит, кто ведёт расписание. Ушёл владелец — она подхватывает расписание, а брошенную
    цепочку продолжает, как раньше."""
    import os
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import aurora_cockpit as ck
        import cron as CRON
        other = os.getppid()                      # живой чужой процесс — «первая панель»
        CRON._write_json(CRON.owner_file(), {"pid": other, "beat": time.time(), "since": "x"})
        run = {"id": "20261007-100000-aaaa", "task": "t1", "name": "цепочка", "trigger": "manual",
               "status": "running", "started": "2026-10-07T10:00:00", "finished": "",
               "items": [{"kind": "route", "project": "/p", "status": "running"}], "panel": other}
        CRON.save_run(run)
        second = CRON.Scheduler(ck, lambda: [])
        assert second.claim() is False and second.passive, "вторая панель взяла расписание"
        assert second.enqueue("t1") == {"ok": False, "error": "other_panel"}
        second.tick()
        assert CRON._read_json(CRON.run_path(run["id"]), {})["status"] == "running"
        assert second.state()["other_panel"] == other
        owner = CRON.Scheduler(ck, lambda: [])
        owner.recover()
        assert CRON._read_json(CRON.run_path(run["id"]), {})["status"] == "running" and not owner.queue, \
            "цепочку живой панели сочли брошенной"
        # Владелец ушёл (отметка старше OWNER_STALE) — вторая панель подхватывает расписание.
        CRON._write_json(CRON.owner_file(), {"pid": other, "beat": time.time() - CRON.OWNER_STALE - 5})
        assert second.claim() and not second.passive and second.claimed
        assert CRON._read_json(CRON.owner_file(), {})["pid"] == os.getpid()
        # Цепочка умершей панели продолжается, как и прежде.
        dead = dict(run, id="20261007-100001-bbbb", panel=2 ** 22 + 12345,
                    items=[{"kind": "route", "project": "/p", "status": "running"}])
        CRON.save_run(dead)
        fresh = CRON.Scheduler(ck, lambda: [])
        fresh.recover()
        got = CRON._read_json(CRON.run_path(dead["id"]), {})
        assert got["status"] == "interrupted" and got["items"][0]["note"] == "panel_restart"
        assert ("t1", "resume", dead["id"]) in fresh.queue, "брошенную цепочку не продолжили"
        view = (COCKPIT / "modules/cron/view.js").read_text(encoding="utf-8")
        assert "DATA.other_panel" in view
    finally:
        restore()
