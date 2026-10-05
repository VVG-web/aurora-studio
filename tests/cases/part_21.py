"""Проверки движка Aurora, часть 21: боты проекта (1.156.0).

Каркас и помощники — tests/harness.py. Бот — файл `bots/<имя>.md`: шапка YAML (MCP, навыки,
вложения, расписание), тело — промпт. Запускается по кнопке и по расписанию раздела «Cron».
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
import re
import sys

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401


def _b():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib
    return importlib.import_module("bots")


def _project(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / "aurora.config.yaml").write_text("project:\n  name: Demo\n", encoding="utf-8")
    (p / "mcp.json").write_text(json.dumps({"mcpServers": {
        "jira-mcp": {"command": "echo"}, "confluence-mcp": {"command": "echo"}}}), encoding="utf-8")
    sk = p / ".aurora" / "skills" / "evaluation-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: evaluation-skill\n---\nОценивай по разделам шаблона.\n",
                                 encoding="utf-8")
    return p


@test
def test_a_bot_is_a_markdown_file_with_its_setup_in_the_frontmatter(tmp: Path):
    """Бот — `bots/*.md`: шапка с MCP, навыками, вложениями и расписанием, тело — промпт.
    Запись сохраняет чужие поля шапки; файл не выходит за папку `bots/`."""
    B = _b()
    p = _project(tmp)
    r = B.create(p, "Анализ меток", {"mcp": ["jira-mcp"], "cron": "0 9 * * 1-5"}, "Промпт бота")
    assert r["ok"] and r["file"] == "bots/analiz-metok.md", r
    text = (p / r["file"]).read_text(encoding="utf-8")
    assert text.startswith("---\nname: Анализ меток\n") and 'cron: "0 9 * * 1-5"' in text
    assert "mcp:\n  - jira-mcp" in text and text.rstrip().endswith("Промпт бота")
    # Чужое поле шапки и правка тела — при записи из панели поле остаётся на месте.
    (p / r["file"]).write_text(text.replace("enabled: false", "enabled: false\nowner: аналитик"),
                               encoding="utf-8")
    bot = B.read(p, r["file"])
    assert B.write(p, r["file"], {**bot["meta"], "skills": ["evaluation-skill"]}, "Новый промпт")["ok"]
    again = (p / r["file"]).read_text(encoding="utf-8")
    assert "owner: аналитик" in again and "skills:\n  - evaluation-skill" in again
    assert B.parse(again)["meta"]["skills"] == ["evaluation-skill"]
    # Разные записи списков читаются одинаково.
    assert B.parse("---\nname: x\nmcp: [a, \"b c\"]\n---\nтело")["meta"]["mcp"] == ["a", "b c"]
    d = B.duplicate(p, r["file"])
    assert d["ok"] and B.read(p, d["file"])["meta"]["enabled"] is False, "копия сразу встала в расписание"
    n = B.rename(p, d["file"], "Второй бот")
    assert n["ok"] and n["file"] == "bots/vtoroy-bot.md" and not (p / d["file"]).exists()
    assert B.delete(p, n["file"])["ok"] and not (p / n["file"]).exists()
    assert [b["file"] for b in B.list_bots(p)] == [r["file"]]
    for bad in ("../aurora.config.yaml", "bots/../x.md", "bots/sub/x.md", "bots/x.txt"):
        assert B.bot_path(p, bad) is None, f"путь бота вышел за папку bots/: {bad}"
    assert not B.write(p, "../evil.md", {}, "x")["ok"]


@test
def test_bot_schedules_are_cron_expressions(_t):
    """Расписание — пять полей cron или @-имя; ошибка называет поле; день месяца и день недели,
    заданные оба, работают как в cron — хватит одного."""
    B = _b()
    when = lambda e: [t.strftime("%a %d %H:%M") for t in
                      B.next_runs(B.parse_cron(e)[0], datetime(2026, 10, 5, 10, 0), 3)]
    assert when("0 9 * * 1-5") == ["Tue 06 09:00", "Wed 07 09:00", "Thu 08 09:00"]
    assert when("*/20 * * * *") == ["Mon 05 10:20", "Mon 05 10:40", "Mon 05 11:00"]
    assert when("0 9 * * mon") == ["Mon 12 09:00", "Mon 19 09:00", "Mon 26 09:00"]
    assert when("@daily") == ["Tue 06 00:00", "Wed 07 00:00", "Thu 08 00:00"]
    assert when("0 0 13 * 5")[:2] == ["Fri 09 00:00", "Tue 13 00:00"], "день месяца ИЛИ день недели"
    assert B.parse_cron("0 9 * * 7")[0]["dow"] == {0}, "7 — тоже воскресенье"
    for bad, err in (("0 25 * * *", "hour"), ("0 9 * *", "fields"), ("x 9 * * *", "minute"),
                     ("0 9 * * 1-9", "dow"), ("0 9 */0 * *", "dom")):
        assert B.parse_cron(bad)[1] == err, bad
    for _id, expr in B.PRESETS:
        assert not B.parse_cron(expr)[1], expr
    sch = B.schedule({"cron": "0 9 * * 1-5", "enabled": True}, datetime(2026, 10, 5, 10, 0))
    assert sch["on"] and sch["next"][0] == "2026-10-06T09:00"
    assert B.schedule({"cron": "0 99 * * *", "enabled": True})["error"] == "hour"
    # неверное поле называется словами: в командной строке — движком, в панели — каталогом
    assert B.detail_text({"code": "cron_invalid", "detail": "hour"}) == B.CRON_TITLES["hour"]
    for lang in ("ru", "en"):
        cat = json.loads((KIT / f"cockpit/modules/bots/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert {k for k in cat if k.startswith("bots.cron_field.")} == \
            {"bots.cron_field." + f for f in B.CRON_TITLES}, f"поле расписания без надписи ({lang})"


@test
def test_a_bot_runs_with_its_mcp_skills_and_attachments(tmp: Path):
    """Прогон: промпт, навык и текст вложений — в задание; MCP — только серверы бота и сразу;
    файлы результата — в папку прогона; итог — в состояние. Неудача — причиной с советом."""
    B = _b()
    import agent_core as AG
    p = _project(tmp)
    (p / "templates").mkdir()
    (p / "templates" / "t.md").write_text("Раздел «Полнота»", encoding="utf-8")
    r = B.create(p, "Бот", {"mcp": ["jira-mcp"], "skills": ["evaluation-skill"],
                            "attachments": ["templates/t.md"]}, "Сделай оценку задач")
    seen = {}

    def fake(cfg, role, messages, **kw):
        seen.update(kw, messages=messages, role=role)
        Path(kw["outdir"]).mkdir(parents=True, exist_ok=True)
        Path(kw["outdir"], "A-1.md").write_text("оценка", encoding="utf-8")
        return {"ok": True, "text": "Готово: A-1", "model": "m"}
    saved = (AG.config, AG.role_chain)
    AG.config = lambda: {"adapter": "openai_compat", "budget_min": 1}
    AG.role_chain = lambda cfg, role: [1]
    try:
        res = B.run(p, r["file"], call=fake, say=lambda s: None)
        assert res["ok"], res
        assert seen["mcp_only"] == ["jira-mcp"] and seen["mcp_active"] == ["jira-mcp"], seen
        assert seen["tool_calls"] == B.TOOL_CALLS and seen["tools"] is True
        out = Path(seen["outdir"]).resolve()
        assert out.is_relative_to((p / "Workspaces" / "bots" / "bot").resolve()), out
        user = seen["messages"][1]["content"]
        assert "Оценивай по разделам" in user and "Раздел «Полнота»" in user and "Сделай оценку" in user
        assert res["outputs"] == [(out / "A-1.md").relative_to(p.resolve()).as_posix()]
        assert (p / res["report"]).read_text(encoding="utf-8").count("Готово: A-1") == 1
        last = B.last_run(p, r["file"])
        assert last["ok"] and last["report"] == res["report"]
        # Блокирующие замечания — до вызова модели: ночью сервер без ключа не дёргают.
        calls = []
        for meta, code in (({"mcp": ["nope"]}, "mcp_unknown"),
                           ({"skills": ["нет-такого"]}, "skill_unknown"),
                           ({"attachments": ["нет/файла.md"]}, "attachment_missing"),
                           ({"attachments": [".env.aurora.local"]}, "attachment_missing")):
            B.write(p, r["file"], {**B.read(p, r["file"])["meta"], "mcp": [], "skills": [],
                                   "attachments": [], **meta}, "x")
            got = B.run(p, r["file"], call=lambda *a, **k: calls.append(1), say=lambda s: None)
            assert got["problem"]["code"] == code and got["problem"]["actions"], (meta, got)
        (p / ".env.aurora.local").write_text("TOKEN=1\n", encoding="utf-8")
        B.write(p, r["file"], {"name": "Бот", "attachments": [".env.aurora.local"]}, "x")
        assert B.run(p, r["file"], call=lambda *a, **k: calls.append(1),
                     say=lambda s: None)["problem"]["code"] == "attachment_secret"
        assert not calls, "бот с блокирующим замечанием дошёл до модели"
        B.write(p, r["file"], {"name": "Бот"}, "x")
        for fail, code in (({"log": ["№1: timeout"], "timed_out": True}, "timeout"),
                           ({"log": ["модель исчерпала вызовы инструментов (40) и не ответила"]}, "tool_limit"),
                           ({"log": ["у роли worker нет бэкендов: раздел «Модели»"]}, "no_models"),
                           ({"log": ["№1: HTTP 500"]}, "model_failed")):
            got = B.run(p, r["file"], call=lambda *a, f=fail, **k: {"ok": False, **f}, say=lambda s: None)
            assert got["problem"]["code"] == code, (fail, got)
            assert B.last_run(p, r["file"])["code"] == code and (p / got["report"]).is_file()
            shown = B.last_run(p, r["file"])["problem"]
            assert shown["code"] == code and "title" not in shown and "fix" not in shown, \
                "итог прогона несёт русские слова на экран — английская панель их покажет"
        lock = B.state_file(p, r["file"]).with_suffix(".lock")
        lock.write_text("1", encoding="utf-8")
        assert B.run(p, r["file"], call=fake, say=lambda s: None)["problem"]["code"] == "busy"
        lock.unlink()
        AG.role_chain = lambda cfg, role: []
        assert B.run(p, r["file"], call=fake, say=lambda s: None)["problem"]["code"] == "no_models"
    finally:
        AG.config, AG.role_chain = saved


@test
def test_a_bot_sees_only_its_mcp_servers_and_writes_only_into_its_run_folder(tmp: Path):
    """`mcp_only` оставляет в вызове только серверы бота; `save_output` пишет в папку
    прогона и ни на шаг из неё."""
    import agent_core as AG
    p = _project(tmp)
    sent = {}

    def transport(kind, b, payload, timeout):
        if kind != "chat":
            return 404, None, "", 0.0
        sent.update(payload)
        return 200, {"choices": [{"message": {"content": "ok"}}]}, "", 0.1
    cfg = {"chains": {"llm": {"worker": [{"n": 1, "url": "http://m.example/v1", "key": "",
                                          "model": "m", "models": {"worker": "m"}}]}},
           "backends": [{"n": 1, "url": "http://m.example/v1", "key": "", "models": {"worker": "m"},
                         "context": 0, "chat": True}],
           "request_timeout": 30, "thinking_roles": {}}
    cwd = os.getcwd()
    os.chdir(p)
    try:
        AG.DOWN.clear()
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "x"}], transport=transport,
                         sleep=lambda s: None, tools=True, guard_text=["x"],
                         mcp_active=["jira-mcp"], mcp_only=["jira-mcp"],
                         outdir=str(p / "Workspaces/bots/b/1"), tool_calls=40)
    finally:
        os.chdir(cwd)
    assert r["ok"], r
    assert list(sent["mcp"]["mcpServers"]) == ["jira-mcp"], "бот увидел чужие MCP-серверы"
    assert Path(sent["outdir"]).parts[-4:] == ("Workspaces", "bots", "b", "1"), \
        f"папка прогона не дошла до адаптера: {sent.get('outdir')}"
    assert sent["tool_calls"] == 40, "предел вызовов инструментов бота не дошёл до адаптера"
    assert {"outdir", "tool_calls"} <= AG.ADAPTER_ONLY, "служебные поля ушли бы в HTTP-запрос"
    sys.path.insert(0, str(SCRIPTS / "agents"))
    import importlib
    adapter = importlib.import_module("pydantic_ai_adapter")

    class Agent:
        tools = {}

        def tool_plain(self, fn):
            self.tools[fn.__name__] = fn
            return fn
    ag = Agent()
    out = tmp / "run"
    adapter.register_output(ag, str(out))
    path = ag.tools["save_output"]("../../evil", "x")
    assert Path(path).resolve().parent == out.resolve() and Path(path).name == "evil.md", path
    assert not (tmp / "evil.md").exists()


@test
def test_bots_with_a_schedule_are_cron_tasks(tmp: Path):
    """Бот с расписанием — задание раздела «Cron»: запускается `bot:run` по минуте cron,
    опоздание догоняется в пределах `GRACE`, свежесохранённый не срабатывает задним числом;
    включение из «Cron» пишется в файл бота."""
    B = _b()
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    CRON = importlib.import_module("cron")
    restore = set_home(tmp / "home")
    try:
        p = _project(tmp)
        r = B.create(p, "Бот", {"cron": "0 9 * * 1-5", "enabled": True}, "x")
        B.create(p, "Без расписания", {}, "x")
        tasks = CRON.bot_tasks([{"path": str(p), "name": "Demo"}])
        assert len(tasks) == 1, tasks
        t = tasks[0]
        assert t["id"] == B.bot_id(str(p), r["file"]) and t["source"] == "bot"
        assert t["steps"] == [{"kind": "command", "project": str(p), "cmd": "bot:run",
                               "args": [f"--bot={r['file']}", "--trigger=cron"]}]
        assert CRON.bot_slot(t, datetime(2026, 10, 6, 9, 0, 30)) == datetime(2026, 10, 6, 9, 0)
        assert CRON.bot_slot(t, datetime(2026, 10, 6, 9, 14)) == datetime(2026, 10, 6, 9, 0)
        assert CRON.bot_slot(t, datetime(2026, 10, 6, 9, 16)) is None, "опоздание больше GRACE"
        assert CRON.bot_slot(t, datetime(2026, 10, 10, 9, 0)) is None, "суббота"

        class Ck:
            DEFAULT_LANG = "ru"
        sched = CRON.Scheduler(Ck(), lambda: [{"path": str(p), "name": "Demo"}])
        assert sched.find(t["id"])["bot"] == r["file"], "очередь не нашла бота по номеру"
        fired = []
        sched.enqueue = lambda tid, trig="manual", resume="": fired.append((tid, trig)) or {"ok": True}
        sched._next = lambda: None
        sched.tick(datetime(2026, 10, 6, 9, 0, 20))
        sched.tick(datetime(2026, 10, 6, 9, 0, 40))
        assert fired == [(t["id"], "schedule")], fired
        CRON.mark_bot_past(t, datetime(2026, 10, 7, 9, 3))
        fired.clear()
        sched.tick(datetime(2026, 10, 7, 9, 4))
        assert not fired, "бот, сохранённый в 9:03, сработал за 9:00 задним числом"
        ck = importlib.import_module("aurora_cockpit")
        res = ck.cron_action("toggle", {"id": t["id"], "enabled": False}, [{"path": str(p), "name": "Demo"}])
        assert res["ok"] and B.read(p, r["file"])["meta"]["enabled"] is False
        assert ck.cron_action("delete", {"id": t["id"]}, [{"path": str(p), "name": "Demo"}])["ok"] is False, \
            "бота удалили из «Cron» — удалять его можно только в «Ботах»"
    finally:
        restore()


@test
def test_the_jira_example_can_be_created_and_scheduled(tmp: Path):
    """Пример из задания: анализ задач Jira с меткой — MCP jira и confluence, навык
    оценки, шаблон вложением, по будням в 9:00. Шаблон кладётся, если его нет."""
    B = _b()
    p = _project(tmp)
    r = B.create_example(p)
    assert r["ok"] and r["template"] == "templates/evaluation-template.md"
    bot = B.read(p, r["file"])
    m = bot["meta"]
    assert m["mcp"] == ["jira-mcp", "confluence-mcp"] and m["skills"] == ["evaluation-skill"]
    assert m["attachments"] == ["templates/evaluation-template.md"] and m["cron"] == "0 9 * * 1-5"
    assert "../templates/evaluation-template.md" in bot["body"], "промпт не ссылается на вложение"
    assert not B.validate(p, m, bot["body"]), "пример с первого раза с замечаниями"
    B.write(p, r["file"], {**m, "enabled": True}, bot["body"])
    sch = B.list_bots(p)[0]["schedule"]
    assert sch["on"] and len(sch["next"]) == 3


@test
def test_the_bots_section_edits_like_files_and_attaches_like_production(_t):
    """Раздел «Боты» — в проекте; промпт — тем же редактором, что «Файлы» (Vditor, тот же
    вид); вложения — подбором файлов и папок проекта, как в «Продуктивности»; «Добавить в
    cron» — одним нажатием; Cron ведёт к боту в его проекте."""
    mod = KIT / "cockpit/modules/bots"
    meta = json.loads((mod / "module.json").read_text(encoding="utf-8"))
    assert meta["group"] == "project" and meta["needs"] == ["project"] and "bot:run" in meta["commands"]
    view = (mod / "view.js").read_text(encoding="utf-8")
    for need, why in (("ctx.ui.editor(", "промпт правится не редактором «Файлов»"),
                      ('"/api/context/suggest?"', "вложения подбираются не как в «Продуктивности»"),
                      ("function insertLink(", "ссылку на вложение в промпт не вставить"),
                      ("async function addToCron(", "нет «Добавить в cron»"),
                      ('checklist(ctx, "mcp"', "MCP-серверы не выбрать"),
                      ('checklist(ctx, "skills"', "навыки не выбрать"),
                      ('post(ctx, "/api/bots/validate"', "поля проверяются только при запуске"),
                      ('ctx.run("bot:run"', "бота не запустить вручную"),
                      ('"/api/bots/duplicate"', "бота не скопировать"), ('"/api/bots/rename"', "бота не переименовать"),
                      ('"/api/bots/delete"', "бота не удалить"), ("lastChip(ctx", "в списке нет итога прогона"),
                      ("scheduleChip(ctx", "в списке нет расписания")):
        assert need in view, why
    assert not re.search(r'["\'][^"\'\n]*[а-яё][^"\'\n]*["\']', re.sub(r"//[^\n]*|/\*.*?\*/", "", view, flags=re.S)), \
        "русская надпись в коде раздела — мимо каталога строк"
    add = view[view.index("async function addToCron("):view.index("async function runNow(")]
    assert add.index('"/api/bots/validate"') < add.index("FORM.enabled = true"), \
        "«Добавить в cron» ставит в расписание неверное выражение, не проверив его"
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    ed = panel[panel.index("async function markdownEditor("):panel.index("async function openProject(")]
    assert "ensureVditor()" in ed and '"aurora-editor-mode"' in ed and "new Vditor(" in ed
    assert "editor: markdownEditor" in panel and "openProject," in panel
    cron = (KIT / "cockpit/modules/cron/view.js").read_text(encoding="utf-8")
    assert 'ctx.openProject(task.project, "bots", {file: task.bot})' in cron
    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for route in ('"/api/bots"', '"/api/bots/file"', '"/api/bots/save"', '"/api/bots/validate"',
                  '"/api/bots/example"'):
        assert route in ck, route
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "scripts/bots.py" in man, "бот не запустится в проекте: движка ботов нет в манифесте"
