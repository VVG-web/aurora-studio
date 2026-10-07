"""Проверки движка Aurora, часть 26: роли по делу, своя модель, MCP Авроры для ботов,
публикация в Jira по шагам, Аврора в ассистентах машины (1.162.0).

Каркас и помощники — tests/harness.py. Промпт бота ревью ссылался на команды `next`,
`note`, `score`, `submit`, переменные окружения и прямые REST-запросы, которых у бота нет;
роли моделей назывались не по делу; выбор модели в «Спросить» с 1.153.0 молча не действовал;
строка «Atlassian MCP в Cursor» проверяла настройку чужого редактора.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import sys

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _cfg():
    _path()
    import agent_core as AG
    import model_config as MC
    data = MC.normalize({
        "providers": [{"id": "gw1", "name": "Шлюз", "url": "http://gw1.example.com/v1"},
                      {"id": "gw2", "name": "Запасной", "url": "http://gw2.example.com/v1"}],
        "capabilities": {"llm": {"roles": [
            {"id": "worker", "name": "Разбор и тезисы",
             "backends": [{"provider": "gw1", "model": "big"}, {"provider": "gw2", "model": "small"}]},
            {"id": "qa", "name": "Мой Момус", "backends": [{"provider": "gw2", "model": "small"}]}]}}})[0]
    return AG, MC, data


@test
def test_engine_roles_are_named_by_what_they_do(_t):
    """Не переименованная человеком роль получает имя по делу; своё имя человека не трогаем."""
    _AG, MC, data = _cfg()
    roles = {r["id"]: r["name"] for r in data["capabilities"]["llm"]["roles"]}
    assert roles["worker"] == "Писатель: разбор, тезисы, ответы", roles
    assert roles["qa"] == "Мой Момус", "своё имя роли человека переписано"
    assert roles["critic"] == "Критик: проверка решений" and roles["planner"].startswith("Планировщик:")
    assert dict(MC.ENGINE_ROLES["llm"])["qa"] == "Момус: проверка опоры и ревью"


@test
def test_a_pinned_model_answers_alone_without_spares(_t):
    """«Провайдер/модель» — ровно эта модель для названной роли, без запасных; чужой провайдер
    — отказ; список выбора — модели ролей по провайдерам."""
    AG, MC, data = _cfg()
    cfg = MC.to_config(data)
    choices = AG.model_choices(cfg)
    assert [(c["provider"], c["models"]) for c in choices] == [("gw1", ["big"]), ("gw2", ["small"])]
    pinned = AG.pin_model(cfg, "gw2/any-model:7b", ("worker",))
    chain = AG.role_chain(pinned, "worker")
    assert len(chain) == 1 and chain[0]["model"] == "any-model:7b" and chain[0]["n"] == 2
    assert chain[0]["fallback"] is False and pinned["pinned"] == "gw2/any-model:7b"
    assert len(AG.role_chain(cfg, "worker")) == 2, "закрепление испортило исходную настройку"
    assert AG.role_chain(pinned, "qa") == AG.role_chain(cfg, "qa"), "Момус потерял свою роль"
    for bad in ("nope/x", "gw1", "/x"):
        try:
            AG.pin_model(cfg, bad)
        except ValueError:
            continue
        raise AssertionError(f"«{bad}» принят как модель")
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert '"--role"' in src and "momus=not a.no_momus, role=role" in src


@test
def test_a_bot_can_pick_a_model_instead_of_a_role(tmp: Path):
    """Поле `model` бота: переживает запись и чтение, неизвестный провайдер — блокирующее
    замечание, прогон идёт ровно этой моделью."""
    _path()
    import agent_core as AG
    import bots as B
    _a, MC, data = _cfg()
    p = tmp / "proj"
    p.mkdir()
    (p / "aurora.config.yaml").write_text("project:\n  name: Demo\n", encoding="utf-8")
    r = B.create(p, "Обзор", {"model": "gw1/qwen/qwen3:32b"}, "Сделай обзор")
    meta = B.read(p, r["file"])["meta"]
    assert meta["model"] == "gw1/qwen/qwen3:32b", meta
    saved = (B.model_choices, B.llm_roles, AG.config, AG.call_role, AG.venv_status)
    seen = {}
    B.model_choices = lambda: AG.model_choices(MC.to_config(data))
    B.llm_roles = lambda: [{"id": "worker", "name": "", "models": ["big"]}]
    AG.config = lambda: MC.to_config(data)
    AG.venv_status = lambda: (True, "")

    def fake(cfg, role, messages, **kw):
        seen["chain"] = [(b["provider"], b["model"]) for b in AG.role_chain(cfg, role)]
        return {"ok": True, "text": "готово", "model": "m", "via": "pydantic_ai", "tools_called": []}
    AG.call_role = fake
    try:
        probs = B.validate(p, {**meta, "model": "nope/x"}, "x", mcp=[], skills=[])
        assert any(x["code"] == "model_unknown" for x in probs) and "model_unknown" in B.BLOCKING
        res = B.run(p, r["file"], say=lambda s: None)
        assert res["ok"], res
        assert seen["chain"] == [("gw1", "qwen/qwen3:32b")], seen
        report = (p / res["report"]).read_text(encoding="utf-8")
        assert "«gw1/qwen/qwen3:32b» (выбрана, без запасных)" in report
    finally:
        B.model_choices, B.llm_roles, AG.config, AG.call_role, AG.venv_status = saved


def _mcp(project, name, args):
    _path()
    import aurora_mcp as M
    try:
        return M.call_tool(str(project), name, args), False
    except M.ToolError as e:
        return str(e), True


@test
def test_the_aurora_mcp_gives_bots_time_memory_and_a_lock(tmp: Path):
    """Время, память между прогонами и замок — инструменты MCP Авроры; бот видит сервер
    `aurora` среди своих, а свой одноимённый сервер человека сильнее встроенного."""
    root = make_project(tmp)
    _path()
    import agent_core as AG
    import aurora_mcp as M
    names = {t["name"] for t in M.TOOLS}
    for need in ("time_now", "state_get", "state_put", "lock_acquire", "review_page",
                 "review_score", "jira_search", "jira_publish", "confluence_find",
                 "confluence_page", "atlassian_check"):
        assert need in names, need
        assert any(t["name"] == need and "project" in t["inputSchema"]["required"]
                   for t in M.TOOLS_ALL), f"{need} без проекта в режиме «все проекты»"
    out, err = _mcp(root, "time_now", {})
    assert not err and "utc" in json.loads(out)
    assert _mcp(root, "state_put", {"space": "бот", "key": "ABC-1", "value": {"step": 2}})[0].startswith("Сохранено")
    assert json.loads(_mcp(root, "state_get", {"space": "бот", "key": "ABC-1"})[0]) == {"step": 2}
    assert "ABC-1" in _mcp(root, "state_list", {"space": "бот"})[0]
    assert _mcp(root, "state_delete", {"space": "бот", "key": "ABC-1"})[0].startswith("Удалено")
    assert _mcp(root, "lock_acquire", {"name": "ревью", "minutes": 5})[0].startswith("Замок")
    text, err = _mcp(root, "lock_acquire", {"name": "ревью"})
    assert err and "занят" in text, "второй прогон взял занятый замок"
    assert _mcp(root, "lock_release", {"name": "ревью"})[0].startswith("Замок")
    assert (root / ".aurora/state/mcp").is_dir(), "память не в состоянии движка (вне git)"
    servers = AG.mcp_config(str(root), with_aurora=True)["mcpServers"]
    assert "aurora" in servers and "--project" in servers["aurora"]["args"]
    assert AG.mcp_config(str(root)) == {}, "встроенный сервер появился без просьбы"
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"aurora": {"command": "mine"}}}),
                                   encoding="utf-8")
    assert AG.mcp_config(str(root), with_aurora=True)["mcpServers"]["aurora"]["command"] == "mine"


@test
def test_review_tools_score_by_the_template_and_one_run_counts(tmp: Path):
    """Ревью: чек-лист и оценка готовых ответов — кодом по шаблону; один годный прогон
    засчитывается (до 1.162.0 `--runs 1` давал «не определено» на каждый вопрос)."""
    root = make_project(tmp)
    _path()
    import review_run as R
    import shutil
    shutil.copy(KIT / "scaffold/TemplatesCommon/review_v2.0.md", root / "TemplatesCommon" / "review_v2.0.md")
    (root / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text("1.162.0\n", encoding="utf-8")
    out, err = _mcp(root, "review_checklist", {"profile": "us"})
    assert not err and json.loads(out)["questions"][0]["id"] == "US-01", out[:200]
    out, err = _mcp(root, "review_score", {"profile": "us", "answers": {
        "US-01": {"v": "yes"}, "US-02": {"v": "no", "evidence": "цитата", "fix": "так"}}})
    data = json.loads(out)
    assert not err and data["summary"].startswith("AI review result") and "US-02" in data["failed_questions"]
    cl = R.load_checklist(str(root / "TemplatesCommon" / "review_v2.0.md"))
    ids = [c["id"] for c in cl["profiles"]["us"] if c["decider"] != "код"]
    run = {"ok": True, "checks": {i: {"v": "yes", "evidence": "", "fix": ""} for i in ids}}
    votes = R.vote(cl, "us", [run], {"marks": [], "candidates": []})
    assert all(votes[i]["v"] == "yes" for i in ids), "один прогон дал «не определено»"
    abstain = {"ok": True, "checks": {i: {"v": "unknown", "evidence": "", "fix": ""} for i in ids}}
    two = R.vote(cl, "us", [run, abstain], {"marks": [], "candidates": []})
    assert two[ids[0]]["v"] == "unknown", "при двух прогонах один голос решил вопрос"


@test
def test_jira_publish_goes_step_by_step_and_needs_permission(tmp: Path):
    """Публикация: вложение → комментарий → метки; не легло вложение — метки не трогаются;
    повтор не грузит то же вложение; запись — только с разрешения проекта; пробный прогон —
    всегда."""
    root = make_project(tmp)
    _path()
    import atlassian_ops as A
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    req = {"op": "jira_publish", "issue": "ABC-7", "content": "отчёт", "filename": "r.md",
           "comment": "итог", "labels_add": ["done"], "labels_remove": ["todo"]}
    try:
        A.run(dict(req), str(root))
        raise AssertionError("записал без разрешения проекта")
    except A.OpError as e:
        assert "jira_write" in str(e)
    calls = []

    class Fake:
        def __init__(self, fail=None, have=()):
            self.fail, self.have = fail, list(have)

        def get(self, path):
            return {"fields": {"attachment": [{"filename": n} for n in self.have]}}

        def send(self, method, path, body=None, files=None, timeout=30):
            what = "attach" if files else "comment" if path.endswith("/comment") else "labels"
            calls.append(what)
            if what == self.fail:
                import urllib.error
                raise urllib.error.HTTPError(path, 500, "boom", {}, None)
            return {}
    saved = A.jira
    try:
        A.jira = lambda root="": Fake()
        dry = A.run({**req, "dry_run": True}, str(root))
        assert dry["ok"] and not calls and len(dry["steps"]) == 3, dry
        cfg.write_text(base + "\nmcp:\n  jira_write: true\n", encoding="utf-8")
        assert A.settings(str(root))["jira_write"] is True
        out = A.run(dict(req), str(root))
        assert out["ok"] and calls == ["attach", "comment", "labels"], (out, calls)
        calls.clear()
        A.jira = lambda root="": Fake(fail="attach")
        out = A.run(dict(req), str(root))
        assert not out["ok"] and calls == ["attach"], "метки сменились без вложения"
        calls.clear()
        A.jira = lambda root="": Fake(have=["r.md"])
        out = A.run(dict(req), str(root))
        assert out["ok"] and calls == ["comment", "labels"], "то же вложение загружено второй раз"
    finally:
        A.jira = saved
        cfg.write_text(base, encoding="utf-8")


@test
def test_aurora_joins_assistants_with_a_backup_and_a_way_back(tmp: Path):
    """Ассистенты машины: Аврора вставляется правкой текста (комментарии человека живы),
    перед правкой — копия, после — проверка; возврат из копии кладёт файл как был."""
    _path()
    import harness_mcp as H
    restore = set_home(tmp / "home")
    try:
        jsonc = ('{\n  // мои настройки\n  "theme": "dark",\n  "context_servers": {\n'
                 '    "other": {"command": "x", "args": [],}, // хвостовая запятая\n  },\n}\n')
        new = H.insert_json(jsonc, "context_servers", "aurora", {"command": "py", "args": []})
        assert "// мои настройки" in new and "// хвостовая запятая" in new
        data = H.load_jsonc(new)
        assert set(data["context_servers"]) == {"other", "aurora"}
        empty = H.insert_json('{"mcpServers": {}}', "mcpServers", "aurora", {"command": "py"})
        assert json.loads(empty)["mcpServers"]["aurora"]["command"] == "py"
        bare = H.insert_json('{"a": 1}', "mcp", "aurora", {"type": "local"})
        assert json.loads(bare) == {"mcp": {"aurora": {"type": "local"}}, "a": 1}
        h = H.harness("hermes")
        yml = H.insert_yaml("model: x\nmcp_servers:\n  other:\n    command: x\n", h, "py", ["s.py"])
        assert H.yaml_has(yml, "mcp_servers") and "  other:" in yml
        cursor = Path(H.config_path(H.harness("cursor")))
        cursor.parent.mkdir(parents=True)
        original = '{\n  "mcpServers": {\n    "mine": {"command": "z"}\n  }\n}\n'
        cursor.write_text(original, encoding="utf-8")
        rows = {r["id"]: r for r in H.detect(versions=False)}
        assert rows["cursor"]["found"] and rows["cursor"]["aurora"] is False
        out = H.add("cursor")
        assert out["ok"] and H.has_aurora(H.harness("cursor"), str(cursor)) is True
        assert H.add("cursor")["done"].startswith("Аврора уже"), "второе подключение правило файл"
        assert H.list_backups("cursor"), "копии перед правкой нет"
        back = H.restore("cursor")
        assert back["ok"] and cursor.read_text(encoding="utf-8") == original, "возврат не тот"
        cont = H.harness("continue")
        Path(H.home(), ".continue").mkdir()
        H.add("continue")
        assert Path(H.config_path(cont)).is_file()
        H.restore("continue")
        assert not Path(H.config_path(cont)).exists(), "созданный файл не убран возвратом"
    finally:
        restore()
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert "Atlassian MCP в Cursor\"" not in src, "строка чужого редактора вернулась в «Установку»"
    assert '"/api/harness"' in src and "/api/harness/add" in src
    view = (COCKPIT / "modules/install/view.js").read_text(encoding="utf-8")
    assert "function harnessCard(" in view and '"/api/harness/restore"' in view \
        and 'act("restore"' in view, "в «Установке» нет подключения и возврата из копии"
    cat = json.loads((KIT / "scripts/harnesses.json").read_text(encoding="utf-8"))["harnesses"]
    ids = {h["id"] for h in cat}
    for need in ("claude-code", "cursor", "codex", "opencode", "continue", "kilo-code", "kilo-cli",
                 "jcode", "pi", "hermes", "deepseek", "zed", "gemini", "vscode"):
        assert need in ids, f"в каталоге нет {need}"


@test
def test_the_old_backend_pick_now_pins_that_providers_model(_t):
    """«Бэкенд №N» в «Спросить»: с 1.153.0 выбор молча не действовал (цепочки ролей его не
    читали). Теперь он закрепляет модель роли на этом провайдере; чужой номер — отказ."""
    _path()
    import agent_runner as R
    AG, MC, data = _cfg()
    cfg = MC.to_config(data)
    pinned = R.ask_cfg(cfg, "worker", backend=2)
    assert [(b["provider"], b["model"]) for b in AG.role_chain(pinned, "worker")] == [("gw2", "small")]
    assert R.ask_cfg(cfg, "worker") is cfg, "без выбора настройка подменена"
    for kw in ({"backend": 9}, {"model": "nope/x"}):
        try:
            R.ask_cfg(cfg, "worker", **kw)
        except ValueError:
            continue
        raise AssertionError(f"{kw} принят молча")


@test
def test_a_second_panel_keeps_the_running_marks_of_a_live_one(tmp: Path):
    """Новая панель снимает отметки «идёт» только мёртвых панелей: 7.10.2026 вторая панель
    стёрла файл целиком посреди ночной цепочки, и цепочка выглядела законченной."""
    _path()
    import aurora_cockpit as ck
    import subprocess
    saved = ck.RUNNING
    ck.RUNNING = str(tmp / "running.json")
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        Path(ck.RUNNING).write_text(json.dumps({
            "a": {"cmd": "cron: ночь", "project": "", "since": "", "panel": live.pid},
            "b": {"cmd": "kb:lint", "project": "X", "since": "", "panel": 999999},
            "c": {"cmd": "старая запись без панели", "project": "X", "since": ""}}),
            encoding="utf-8")
        ck.drop_dead_running()
        left = json.loads(Path(ck.RUNNING).read_text(encoding="utf-8"))
        assert set(left) == {"a"}, f"снято не то: {left}"
        ck.mark_running("d", "kb:embed", "/p/X", True)
        assert json.loads(Path(ck.RUNNING).read_text(encoding="utf-8"))["d"]["panel"] == os.getpid()
    finally:
        live.kill()
        ck.RUNNING = saved
