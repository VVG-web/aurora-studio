"""Проверки движка Aurora, часть 37: согласие владельца машины на исполняемое содержимое (ADR-026).

Каркас и помощники — tests/harness.py. Бот приезжает через git и исполняется на машине каждого
участника с его токенами и квотой модели; скрипт движка из клона проекта запускается панелью при
старте. Новое или изменённое содержимое не исполняется, пока владелец машины не согласился:
согласие — запись «проект + файл + SHA-256» в `~/.aurora/consent.json`, вне репозитория.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import os
import shutil
import sys
from datetime import datetime

from harness import KIT, SCRIPTS, run, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _own_consent(tmp: Path):
    """Своё хранилище согласий на время проверки. → функция возврата прежнего."""
    saved = os.environ.get("AURORA_CONSENT_HOME")
    os.environ["AURORA_CONSENT_HOME"] = str(tmp / "owner-home")

    def restore():
        if saved is None:
            os.environ.pop("AURORA_CONSENT_HOME", None)
        else:
            os.environ["AURORA_CONSENT_HOME"] = saved
    return restore


def _project(tmp: Path) -> Path:
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / "aurora.config.yaml").write_text("project:\n  name: Demo\n", encoding="utf-8")
    return p


def _git_bot(B, p: Path, name: str = "pulled", text: str | None = None) -> str:
    """Файл бота так, как он приходит из git: мимо панели и без согласия."""
    (p / "bots").mkdir(exist_ok=True)
    rel = f"bots/{name}.md"
    (p / rel).write_text(text or "---\nname: Pulled\ncron: \"* * * * *\"\nenabled: true\n---\nСделай дело\n",
                         encoding="utf-8")
    return rel


@test
def test_a_bot_from_git_is_not_run_until_the_owner_approves_it(tmp: Path):
    """Новый бот не исполняется ни в одном из путей запуска; согласие — на хеш текста: один
    байт правки снимает его; подтверждённый и неизменный бот исполняется."""
    _path()
    import agent_core as AG
    B = importlib.import_module("bots")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        rel = _git_bot(B, p)
        calls = []

        def fake(cfg, role, messages, **kw):
            calls.append(1)
            return {"ok": True, "text": "готово", "model": "m", "via": "pydantic_ai", "tools_called": []}
        saved = (AG.config, AG.role_chain, AG.venv_status, B.llm_roles)
        AG.config = lambda: {"adapter": "pydantic_ai", "budget_min": 1}
        AG.role_chain = lambda cfg, role: [1]
        AG.venv_status = lambda: (True, "")
        B.llm_roles = lambda: [{"id": "worker", "name": "", "models": ["m"]}]
        try:
            for trigger in ("cron", "manual"):
                got = B.run(p, rel, trigger=trigger, call=fake, say=lambda s: None)
                assert not got["ok"] and got["problem"]["code"] == "not_approved", got
            assert B.list_bots(p)[0]["consent"] == "new"
            assert not calls, "бот без согласия дошёл до модели"
            B.approve(p, rel)
            assert B.run(p, rel, call=fake, say=lambda s: None)["ok"] and calls
            # изменение одного байта (git pull от коллеги) снимает согласие
            f = p / rel
            f.write_text(f.read_text(encoding="utf-8") + " ", encoding="utf-8")
            calls.clear()
            got = B.run(p, rel, call=fake, say=lambda s: None)
            assert got["problem"]["code"] == "not_approved" and got["problem"]["state"] == "changed", got
            assert B.list_bots(p)[0]["consent"] == "changed" and not calls
            assert B.consent_view(p, rel)["diff"] == "" or "+" in B.consent_view(p, rel)["diff"]
            B.approve(p, rel)
            assert B.run(p, rel, call=fake, say=lambda s: None)["ok"]
        finally:
            AG.config, AG.role_chain, AG.venv_status, B.llm_roles = saved
        # хранилище — на машине владельца, а не в проекте
        reg = Path(os.environ["AURORA_CONSENT_HOME"]) / "consent.json"
        assert reg.is_file()
        assert not list(p.rglob("consent*")), "согласие лежит в репозитории проекта"
        assert json.loads(reg.read_text(encoding="utf-8"))["items"], "записи согласия нет"
        # те же байты в другом проекте — это другой файл: согласие не переходит
        q = tmp / "other"
        q.mkdir()
        (q / "aurora.config.yaml").write_text("project:\n  name: Other\n", encoding="utf-8")
        _git_bot(B, q, text=(p / rel).read_text(encoding="utf-8"))
        assert B.list_bots(q)[0]["consent"] == "new"
    finally:
        restore()


@test
def test_the_owner_s_own_edits_in_the_panel_keep_the_approval_but_pulled_files_do_not(tmp: Path):
    """Правка в панели переносит согласие на новый текст, если прежний был согласован («одобрить
    свои изменения»); бот из git правкой в панели не согласуется; копия несогласованного — тоже."""
    _path()
    B = importlib.import_module("bots")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        made = B.create(p, "Свой", {}, "Сделай дело", owner=True)["file"]
        assert B.list_bots(p)[0]["consent"] == "granted", "созданный в панели бот ждёт согласия"
        B.write(p, made, {"name": "Свой", "description": "правка"}, "Другое задание", owner=True)
        assert B.read(p, made)["meta"]["description"] == "правка"
        assert B.list_bots(p)[0]["consent"] == "granted", "правка владельца сняла его согласие"
        B.write(p, made, {"name": "Свой"}, "Правка из git", owner=False)
        assert B.list_bots(p)[0]["consent"] == "changed", "правка мимо панели не сняла согласие"
        dup = B.duplicate(p, made, "", owner=True)["file"]
        assert {b["file"]: b["consent"] for b in B.list_bots(p)}[dup] == "new", \
            "копия несогласованного бота получила согласие"
        pulled = _git_bot(B, p)
        B.write(p, pulled, {"name": "Pulled", "cron": "* * * * *", "enabled": True}, "Своё", owner=True)
        assert {b["file"]: b["consent"] for b in B.list_bots(p)}[pulled] == "new", \
            "бот из git согласован правкой в панели"
        # переименование согласованного бота несёт согласие в новый файл
        B.approve(p, made)
        new = B.rename(p, made, "Новое имя", owner=True)["file"]
        assert new != made and {b["file"]: b["consent"] for b in B.list_bots(p)}[new] == "granted"
        # хеш показанного текста: файл успел измениться — согласия нет
        view = B.consent_view(p, pulled)
        (p / pulled).write_text((p / pulled).read_text(encoding="utf-8") + "\nещё", encoding="utf-8")
        assert B.approve(p, pulled, expect=view["sha256"])["error"] == "changed"
        assert B.approve(p, pulled, expect=B.consent_view(p, pulled)["sha256"])["ok"]
        B.delete(p, pulled)
        reg = json.loads((Path(os.environ["AURORA_CONSENT_HOME"]) / "consent.json").read_text(encoding="utf-8"))
        assert not any(v["file"] == pulled for v in reg["items"].values()), "согласие удалённого бота осталось"
    finally:
        restore()


@test
def test_cron_does_not_queue_a_bot_that_waits_for_approval(tmp: Path):
    """Планировщик не ставит в очередь бота без согласия, не раскрывает его в шаге «все боты» и
    не берёт кнопкой «Запустить»; согласованный запускается; слот, пропущенный в ожидании,
    после согласия не догоняется."""
    _path()
    B = importlib.import_module("bots")
    CRON = importlib.import_module("cron")
    ck = importlib.import_module("aurora_cockpit")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        rel = _git_bot(B, p)
        projects = [{"path": str(p), "name": "Demo"}]
        task = CRON.bot_tasks(projects)[0]
        assert task["enabled"] and task["consent"] == "new", task
        assert CRON.bot_slot(task, datetime(2026, 10, 6, 9, 0, 30)) is None, "бот без согласия получил слот"
        assert CRON.bot_next(task) == ""

        class Ck:
            DEFAULT_LANG = "ru"
        sched = CRON.Scheduler(Ck(), lambda: projects)
        fired = []
        sched.enqueue = lambda tid, trig="manual", resume="": fired.append(tid) or {"ok": True}
        sched._next = lambda: None
        sched.tick(datetime(2026, 10, 6, 9, 0, 20))
        assert not fired, "планировщик поставил в очередь бота без согласия"
        got = [i["bot"] for i in CRON.expand(
            {"order": "projects", "steps": [{"kind": "bot", "project": str(p), "bot": "*"}]}, projects)]
        assert got == [], f"«все боты» раскрылись в бота без согласия: {got}"
        assert ck.cron_action("start", {"id": task["id"]}, projects) == {"ok": False, "error": "not_approved"}
        # согласие через панель; прошедшую минуту расписания не догоняем
        res = ck.bots_action("approve", str(p), {"file": rel})
        assert res["ok"] and res["files"] == [rel], res
        task = CRON.bot_tasks(projects)[0]
        assert task["consent"] == "granted"
        assert CRON.bot_slot(task, datetime.now()) is not None
        # тик в минуту, на которую согласие дано: слот помечен прошедшим при согласии
        sched.tick(datetime.now())
        assert not fired, "после согласия сработал слот, пропущенный в ожидании"
    finally:
        restore()


@test
def test_a_project_engine_script_runs_only_as_a_kit_copy_or_after_approval(tmp: Path):
    """Скрипт из `<проект>/.aurora/scripts`: копия кита (тот же хеш) исполняется; чужой или
    изменённый — нет, вместо него работает скрипт кита, пока владелец не согласится; правка
    после согласия возвращает ожидание."""
    _path()
    AC = importlib.import_module("aurora_common")
    B = importlib.import_module("bots")
    ck = importlib.import_module("aurora_cockpit")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        eng = p / ".aurora" / "scripts"
        eng.mkdir(parents=True)
        kit_copy = SCRIPTS / "aurora_stats.py"
        shutil.copy(kit_copy, eng / "aurora_stats.py")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == eng / "aurora_stats.py", \
            "копия кита в проекте не используется"
        mine = eng / "aurora_stats.py"
        mine.write_text(kit_copy.read_text(encoding="utf-8") + "\nprint('foreign')\n", encoding="utf-8")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == kit_copy, "чужой скрипт исполнился без согласия"
        assert [s["script"] for s in B.pending_scripts(p)] == ["aurora_stats.py"]
        assert B.pending_scripts(p)[0]["state"] == "new"
        # run_capture идёт тем же путём: вместо чужого запускается скрипт кита
        view = B.script_view(p, "aurora_stats.py")
        assert "foreign" in view["text"]
        assert B.approve_script(p, "aurora_stats.py", expect="0" * 64)["error"] == "changed"
        assert B.approve_script(p, "aurora_stats.py", expect=view["sha256"])["ok"]
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == mine and not B.pending_scripts(p)
        mine.write_text(mine.read_text(encoding="utf-8") + "print('more')\n", encoding="utf-8")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == kit_copy, "изменение после согласия не снято"
        assert B.pending_scripts(p)[0]["state"] == "changed"
        assert "more" in B.script_view(p, "aurora_stats.py")["diff"]
        # свой скрипт, которого в ките нет, тоже ждёт согласия; имя не выводит за папку скриптов
        (eng / "own_job.py").write_text("print(1)\n", encoding="utf-8")
        assert AC.trusted_engine_script(p, "own_job.py", str(SCRIPTS / "own_job.py")) == str(SCRIPTS / "own_job.py")
        assert not B.script_view(p, "../x.py")["ok"] and not B.script_view(p, "sub/x.py")["ok"]
        # ядро панели грело бы здоровье этим же путём: ни один путь запуска не берёт проектный скрипт мимо
        src = "".join((SCRIPTS / f).read_text(encoding="utf-8") for f in
                      ("agent_runner.py", "agent_core.py", "aurora_mcp.py", "bot_context.py"))
        assert src.count("trusted_engine_script(") >= 5, "путь запуска берёт скрипт проекта мимо согласия"
        assert '".aurora", "scripts", script' not in src
    finally:
        restore()


@test
def test_approval_is_a_command_for_the_owner_only(tmp: Path):
    """`bots.py --approve` — команда владельца: показывает, что одобряется, и записывает согласие;
    ни агент, ни команды панели (commands.txt) её не имеют."""
    _path()
    B = importlib.import_module("bots")
    AG = importlib.import_module("agent_core")
    p = _project(tmp)
    rel = _git_bot(B, p)
    home = tmp / "owner-home"
    env = {**os.environ, "AURORA_CONSENT_HOME": str(home)}
    import subprocess
    cp = subprocess.run([sys.executable, str(SCRIPTS / "bots.py"), "--approve", "--project", str(p)],
                        capture_output=True, text=True, encoding="utf-8", env=env)
    assert cp.returncode == 2 and not (home / "consent.json").exists(), "согласие без указания бота"
    cp = subprocess.run([sys.executable, str(SCRIPTS / "bots.py"), "--check", "--bot", rel, "--project", str(p)],
                        capture_output=True, text=True, encoding="utf-8", env=env)
    assert "ждёт согласия" in cp.stdout, cp.stdout
    cp = subprocess.run([sys.executable, str(SCRIPTS / "bots.py"), "--approve", "--bot", rel, "--project", str(p)],
                        capture_output=True, text=True, encoding="utf-8", env=env)
    assert cp.returncode == 0 and "Сделай дело" in cp.stdout and "Согласовано: 1" in cp.stdout, cp.stdout
    cp = subprocess.run([sys.executable, str(SCRIPTS / "bots.py"), "--list", "--json", "--project", str(p)],
                        capture_output=True, text=True, encoding="utf-8", env=env)
    assert json.loads(cp.stdout)["bots"][0]["consent"] == "granted"
    log = (home / "consent-log.jsonl").read_text(encoding="utf-8")
    assert '"event": "grant"' in log, "согласие не записано в журнал"
    ok, why = AG.write_allowed("bots.py", ["--approve", "--all"])
    assert not ok and "владелец" in why, why
    rows = [ln.split(" | ") for ln in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines()
            if ln.startswith("bot |")]
    assert rows and not any("--approve" in r[4] or r[1] == "bot:approve" for r in rows), \
        "согласие выдано командой панели"


@test
def test_the_consent_texts_exist_in_both_languages(_t):
    """Раздел «Боты» и «Cron» говорят о согласии на обоих языках, а каждый код замечания
    `not_approved` имеет надпись и совет."""
    _path()
    B = importlib.import_module("bots")
    assert "not_approved" in B.PROBLEMS and "not_approved" in B.problem("not_approved")["code"]
    for lang in ("ru", "en"):
        bots = json.loads((COCKPIT / f"modules/bots/i18n/{lang}.json").read_text(encoding="utf-8"))
        cron = json.loads((COCKPIT / f"modules/cron/i18n/{lang}.json").read_text(encoding="utf-8"))
        for k in ("bots.p.not_approved", "bots.fix.not_approved", "bots.a.approve", "bots.consent.chip",
                  "bots.consent.approve", "bots.consent.show", "bots.consent.scripts_title"):
            assert bots.get(k), f"нет надписи {k} ({lang})"
        for k in ("cron.bot_consent", "cron.bot_review", "cron.err.not_approved"):
            assert cron.get(k), f"нет надписи {k} ({lang})"
    view = (COCKPIT / "modules/bots/view.js").read_text(encoding="utf-8")
    assert '"/api/bots/approve"' in view and '"/api/bots/consent"' in view
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"/api/bots/approve"' in src and '"/api/bots/consent"' in src
