"""Проверки движка Aurora, часть 37: согласие владельца машины на исполняемое содержимое (ADR-026).

Каркас и помощники — tests/harness.py. Бот приезжает через git и исполняется на машине каждого
участника с его токенами и квотой модели; скрипт движка из клона проекта запускается панелью при
старте. Новое или изменённое содержимое не исполняется, пока владелец машины не согласился:
согласие — запись «проект + файл + SHA-256» в `~/.aurora/consent.json`, вне репозитория.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import importlib.util
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
        got = [(i["bot"], i["status"], i["note"]) for i in CRON.expand(
            {"order": "projects", "steps": [{"kind": "bot", "project": str(p), "bot": "*"}]}, projects)]
        assert got == [(rel, "skipped", "not_approved")], \
            f"«все боты»: бот без согласия не пропущен с пометкой (молчаливый пропуск): {got}"
        assert ck.cron_action("start", {"id": task["id"]}, projects) == {"ok": False, "error": "not_approved"}
        # согласие через панель; прошедшую минуту расписания не догоняем
        shown = ck.BOTS.consent_view(p, rel)["sha256"]       # хеш того, что человек видел в карточке
        res = ck.bots_action("approve", str(p), {"file": rel, "sha256": shown})
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
        # панель: ни одного места, которое собирает путь к скрипту проекта в обход script_path
        import re as _re
        for f in sorted(COCKPIT.glob("*.py")):
            bad = _re.findall(r"engine_dir\([^)]*\),\s*[\"']scripts[\"']", f.read_text(encoding="utf-8"))
            assert not bad, f"{f.name}: скрипт проекта берётся мимо script_path: {bad}"
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


# ----------------------------------------------------------------- доработка: папка движка целиком

def _evil(path: Path, marker_env: str = "AURORA_PWN_MARKER") -> None:
    """Подложенный модуль: при импорте или запуске оставляет след в файле из переменной среды."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"import os\nopen(os.environ[{marker_env!r}], 'a').write({path.name!r})\n", encoding="utf-8")


class _Marker:
    """След подложенного кода: файл, в который он пишет; `hit()` — исполнился ли он."""
    def __init__(self, tmp: Path):
        self.path = tmp / "pwned.txt"
        self.saved = os.environ.get("AURORA_PWN_MARKER")
        os.environ["AURORA_PWN_MARKER"] = str(self.path)

    def hit(self) -> bool:
        return self.path.exists()

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)

    def close(self) -> None:
        if self.saved is None:
            os.environ.pop("AURORA_PWN_MARKER", None)
        else:
            os.environ["AURORA_PWN_MARKER"] = self.saved


def _run_script(path, cwd: Path) -> None:
    import subprocess
    subprocess.run([sys.executable, str(path), "--help"], cwd=cwd, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=120)


@test
def test_a_foreign_neighbour_module_blocks_the_whole_project_engine_folder(tmp: Path):
    """Копия кита рядом с чужим `aurora_common.py` не исполняется: Python кладёт папку скрипта
    первой в `sys.path`, и сосед подхватился бы `import`ом. Работает скрипт кита; согласие на
    соседа возвращает копию; байт-код и ссылка на папку согласию не подлежат."""
    _path()
    AC = importlib.import_module("aurora_common")
    B = importlib.import_module("bots")
    ck = importlib.import_module("aurora_cockpit")
    restore = _own_consent(tmp)
    mk = _Marker(tmp)
    try:
        p = _project(tmp)
        eng = p / ".aurora" / "scripts"
        eng.mkdir(parents=True)
        shutil.copy(SCRIPTS / "aurora_stats.py", eng / "aurora_stats.py")
        _evil(eng / "aurora_common.py")
        got = Path(ck.script_path(str(p), "aurora_stats.py"))
        assert got == SCRIPTS / "aurora_stats.py", f"копия кита рядом с чужим модулем исполняется: {got}"
        assert AC.script_consent(p, "aurora_stats.py", str(SCRIPTS / "aurora_stats.py"))["state"] == "blocked"
        assert [s["script"] for s in B.pending_scripts(p)] == ["aurora_common.py"]
        _run_script(got, p)
        assert not mk.hit(), "чужой aurora_common.py исполнился рядом с китовым скриптом"
        # контроль: сам подложенный модуль действительно бьёт, если запустить копию из проекта
        _run_script(eng / "aurora_stats.py", p)
        assert mk.hit(), "проверка не ловит подмену: подложенный модуль не сработал даже напрямую"
        # согласие на соседа — копия снова из проекта; правка соседа снимает согласие со всей папки
        view = B.script_view(p, "aurora_common.py")
        assert B.approve_script(p, "aurora_common.py", expect=view["sha256"])["ok"]
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == eng / "aurora_stats.py"
        (eng / "aurora_common.py").write_text("x = 1\n", encoding="utf-8")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == SCRIPTS / "aurora_stats.py"
        shutil.copy(SCRIPTS / "aurora_common.py", eng / "aurora_common.py")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == eng / "aurora_stats.py", \
            "папка из копий кита не исполняется"
        # байт-код вне __pycache__ подхватывается импортом — папка заперта, согласием не снять
        (eng / "extra_mod.pyc").write_bytes(b"\0\0")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == SCRIPTS / "aurora_stats.py"
        assert [s["state"] for s in B.pending_scripts(p)] == ["extra"]
        assert not B.script_view(p, "extra_mod.pyc")["ok"]
        (eng / "extra_mod.pyc").unlink()
        # на Windows `.pyw` — тоже исходник для import: `json.pyw` в папке скрипта подменил бы stdlib
        (eng / "json.pyw").write_text("x = 1\n", encoding="utf-8")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == SCRIPTS / "aurora_stats.py", \
            "файл .pyw в папке движка не запер папку"
        assert [s["state"] for s in B.pending_scripts(p)] == ["extra"]
        (eng / "json.pyw").unlink()
        # обычный байт-код (по времени) Python сверит с исходником и при расхождении пересоберёт:
        # папку он не запирает; обрывок короче заголовка Python не загрузит вовсе
        (eng / "__pycache__").mkdir()
        (eng / "__pycache__" / "x.cpython-39.pyc").write_bytes(b"\0")
        import py_compile
        py_compile.compile(str(eng / "aurora_stats.py"), doraise=True)
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == eng / "aurora_stats.py", \
            "обычный __pycache__ не должен запирать папку"
        # подпапка agents/ — тоже часть папки движка
        _evil(eng / "agents" / "pydantic_ai_adapter.py")
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == SCRIPTS / "aurora_stats.py"
        assert [s["script"] for s in B.pending_scripts(p)] == ["agents/pydantic_ai_adapter.py"]
        assert B.script_view(p, "agents/pydantic_ai_adapter.py")["ok"]
        assert not B.script_view(p, "agents/../../x.py")["ok"]
    finally:
        mk.close()
        restore()


@test
def test_a_runner_inside_the_project_does_not_trust_its_own_folder(tmp: Path):
    """Раннер лежит в проекте: его «запасной» скрипт — тот же файл проекта, и сравнение с самим
    собой всегда сходилось. Теперь кит берётся по указателю; чужой `kb_fix.py` не согласуется,
    а вместо него возвращается скрипт кита. Кита нет — «совпадение» не засчитывается."""
    _path()
    AC = importlib.import_module("aurora_common")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        eng = p / ".aurora" / "scripts"
        eng.mkdir(parents=True)
        (eng / "kb_fix.py").write_text("print('foreign')\n", encoding="utf-8")
        own = str(eng / "kb_fix.py")                       # fallback раннера из проекта = сам файл
        info = AC.script_consent(p, "kb_fix.py", own)
        assert info["state"] == "new", f"чужой файл сошёлся сам с собой: {info['state']}"
        assert AC.trusted_engine_script(p, "kb_fix.py", own) == str(SCRIPTS / "kb_fix.py")
        shutil.copy(SCRIPTS / "kb_fix.py", eng / "kb_fix.py")
        assert AC.script_consent(p, "kb_fix.py", own)["state"] == "same"
        # кита не найти: сравнивать не с чем, остаётся только согласие
        (eng / "kb_fix.py").write_text("print('foreign')\n", encoding="utf-8")
        saved = AC.kit_root
        AC.kit_root = lambda: None
        try:
            assert AC.script_consent(p, "kb_fix.py", own)["state"] == "new"
            shutil.copy(SCRIPTS / "kb_fix.py", eng / "kb_fix.py")
            assert AC.script_consent(p, "kb_fix.py", own)["state"] == "new", "без кита копия не может быть «той же»"
        finally:
            AC.kit_root = saved
    finally:
        restore()


@test
def test_the_doctor_does_not_import_modules_from_the_project(tmp: Path):
    """Doctor запускается панелью на старте для каждого найденного проекта: `make_kinds.py` и
    `sources_registry.py` из `<проект>/.aurora/scripts` он не импортирует."""
    _path()
    import subprocess
    restore = _own_consent(tmp)
    mk = _Marker(tmp)
    try:
        p = _project(tmp)
        (p / ".aurora").mkdir()
        (p / ".aurora" / "structure_dirs.txt").write_text("Raw\n", encoding="utf-8")
        for name in ("make_kinds.py", "sources_registry.py"):
            _evil(p / ".aurora" / "scripts" / name)
        subprocess.run([sys.executable, str(SCRIPTS / "aurora_doctor.py")], cwd=p, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
        assert not mk.hit(), "doctor исполнил модуль из чужого проекта"
        src = (SCRIPTS / "aurora_doctor.py").read_text(encoding="utf-8")
        assert 'ROOT / ".aurora" / "scripts"' not in src
    finally:
        mk.close()
        restore()


@test
def test_the_panel_health_scripts_of_a_project_need_the_owners_approval(tmp: Path):
    """`kb_lint.py`, `kb_corrections.py` и `kb_graph.py` из проекта панель запускала напрямую
    (исправления человека греются на старте панели). Теперь — через `script_path`: подложенный
    файл не исполняется, работает скрипт кита."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    restore = _own_consent(tmp)
    mk = _Marker(tmp)
    try:
        p = _project(tmp)
        for name in ("kb_lint.py", "kb_corrections.py", "kb_graph.py"):
            _evil(p / ".aurora" / "scripts" / name)
        (p / "Raw" / "corrections").mkdir(parents=True)
        (p / "AuroraKnowledgeDB").mkdir()
        ck.corrections_state(str(p))
        assert not mk.hit(), "kb_corrections.py из проекта исполнился без согласия"
        ck.lint_one(str(p), "AuroraKnowledgeDB/x.md")
        assert not mk.hit(), "kb_lint.py из проекта исполнился без согласия"
        ck.graph_state(str(p), rebuild=True)
        assert not mk.hit(), "kb_graph.py из проекта исполнился без согласия"
    finally:
        mk.close()
        restore()


@test
def test_approve_all_takes_only_the_list_the_owner_saw(tmp: Path):
    """«Одобрить все» привязано к показанному списку: бот, появившийся или изменившийся после
    показа, не одобряется; запрос без списка отклоняется; команда терминала (`--all`) одобряет
    только ботов, а скрипты проекта — отдельным флагом."""
    _path()
    B = importlib.import_module("bots")
    ck = importlib.import_module("aurora_cockpit")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        a = _git_bot(B, p, "a")
        shown = {b["file"]: b["sha256"] for b in B.list_bots(p, with_checks=False)}
        evil = _git_bot(B, p, "evil")                       # появился после показа списка
        res = ck.bots_action("approve", str(p), {"all": True, "expect": shown})
        assert res["ok"] and res["files"] == [a], res
        states = {b["file"]: b["consent"] for b in B.list_bots(p, with_checks=False)}
        assert states[a] == "granted" and states[evil] == "new", states
        # файл изменился после показа — хеш не сходится
        (p / a).write_text((p / a).read_text(encoding="utf-8") + "\nещё\n", encoding="utf-8")
        assert B.approve_all(p, {a: shown[a]}) == []
        # без списка «всех» одобрять нечем
        assert ck.bots_action("approve", str(p), {"all": True})["ok"] is False
        assert ck.bots_action("approve", str(p), {"all": True, "expect": {}})["ok"] is False
        assert B.list_bots(p, with_checks=False)[1]["consent"] != "granted"
        # терминал: --all — только боты
        import subprocess
        eng = p / ".aurora" / "scripts"
        eng.mkdir(parents=True)
        (eng / "own_job.py").write_text("print(1)\n", encoding="utf-8")
        env = {**os.environ}
        cp = subprocess.run([sys.executable, str(SCRIPTS / "bots.py"), "--approve", "--all", "--json",
                             "--project", str(p)], capture_output=True, text=True, encoding="utf-8", env=env)
        done = json.loads(cp.stdout.strip().splitlines()[-1])["approved"]
        assert sorted(done) == sorted([a, evil]), done
        assert [s["script"] for s in B.pending_scripts(p)] == ["own_job.py"], "--all одобрил и скрипты"
        cp = subprocess.run([sys.executable, str(SCRIPTS / "bots.py"), "--approve", "--scripts", "--json",
                             "--project", str(p)], capture_output=True, text=True, encoding="utf-8", env=env)
        assert ".aurora/scripts/own_job.py" in json.loads(cp.stdout.strip().splitlines()[-1])["approved"], cp.stdout
        assert not B.pending_scripts(p)
    finally:
        restore()


@test
def test_consent_store_survives_parallel_grants_and_exact_bytes(tmp: Path):
    """Параллельные согласия не теряются (замок и своё имя временного файла); хеш скрипта
    считается по байтам: разные неверные байты UTF-8 — разные хеши; CRLF и LF — один."""
    _path()
    import threading
    AC = importlib.import_module("aurora_common")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        errs = []

        def grant(i):
            try:
                AC.consent_grant("bot", p, f"bots/b{i}.md", f"текст {i}")
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=grant, args=(i,)) for i in range(24)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert not errs, errs
        assert len(AC.consent_load()) == 24, "параллельные согласия затёрли друг друга"
        assert not list((tmp / "owner-home").glob("*.tmp")), "остался временный файл"
        assert AC.content_digest(b"a\xff") != AC.content_digest(b"a\xfe")
        assert AC.content_digest(b"a\r\nb") == AC.content_digest("a\nb")
    finally:
        restore()


@test
def test_the_agent_cannot_approve_even_with_an_abbreviated_flag(_t):
    """argparse принимает сокращения: `--appr` для агента — то же согласие, что `--approve`."""
    _path()
    AG = importlib.import_module("agent_core")
    for flag in ("--approve", "--appr", "--ap", "--approve=1"):
        ok, why = AG.write_allowed("bots.py", [flag, "--all"])
        assert not ok and "владелец" in why, (flag, why)
    assert AG.write_allowed("bots.py", ["--list"])[0]


@test
def test_the_doctor_reports_bots_and_engine_files_waiting_for_consent(tmp: Path):
    """Бот и файл движка без согласия не молчат: doctor называет их, а не оставляет
    человеку гадать, почему ночью ничего не запустилось."""
    _path()
    import subprocess
    B = importlib.import_module("bots")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        _git_bot(B, p)
        (p / ".aurora" / "scripts").mkdir(parents=True)
        (p / ".aurora" / "scripts" / "own_job.py").write_text("print(1)\n", encoding="utf-8")
        cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_doctor.py")], cwd=p, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120,
                            env={**os.environ, "AURORA_CONSENT_HOME": str(tmp / "owner-home")})
        assert "ботов ждёт согласия" in cp.stdout and "файлов движка проекта" in cp.stdout, cp.stdout
    finally:
        restore()


# ----------------------------------------------------------------- доработка 3: байт-код, отчёты, вложенные имена

def _plant_pyc(eng: Path, name: str, hashed: bool) -> Path:
    """Подложить в `__pycache__` байт-код, исполняющий `_evil`, под настоящий исходник `name`.
    `hashed` — с хешем «не проверять»; иначе — с отметкой времени и размером исходника, как
    подгоняет их архив с сохранением mtime."""
    import py_compile
    src = eng / name
    evil = eng.parent / "evil_src.py"
    code = "import os\nopen(os.environ['AURORA_PWN_MARKER'], 'a').write('pyc')\n"
    pad = max(src.stat().st_size - len(code.encode("utf-8")), 0)
    evil.write_text(code + "#" * (pad - 1) + "\n" if pad else code, encoding="utf-8")
    st = src.stat()
    os.utime(evil, ns=(st.st_atime_ns, st.st_mtime_ns))
    # префикс процесса сбил бы `cache_from_source` в чужую папку: имя собираем сами
    cfile = eng / "__pycache__" / f"{src.stem}.{sys.implementation.cache_tag}.pyc"
    cfile.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(evil), cfile=str(cfile), doraise=True,
                       invalidation_mode=(py_compile.PycInvalidationMode.UNCHECKED_HASH if hashed
                                          else py_compile.PycInvalidationMode.TIMESTAMP))
    evil.unlink()
    return cfile


@test
def test_a_planted_pyc_neither_runs_nor_passes_for_a_clean_folder(tmp: Path):
    """Подложенный `.pyc` под настоящий исходник: с хешем «не проверять» он запирает папку
    (согласию не подлежит), с подогнанными временем и размером — не исполняется, потому что
    запуски движка идут с префиксом байт-кода машины. Контроль: без префикса он бьёт."""
    _path()
    AC = importlib.import_module("aurora_common")
    B = importlib.import_module("bots")
    ck = importlib.import_module("aurora_cockpit")
    restore = _own_consent(tmp)
    mk = _Marker(tmp)
    try:
        p = _project(tmp)
        eng = p / ".aurora" / "scripts"
        eng.mkdir(parents=True)
        for f in ("aurora_stats.py", "aurora_common.py"):
            shutil.copy2(SCRIPTS / f, eng / f)
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == eng / "aurora_stats.py"
        # 1. байт-код с хешем: папка заперта, в списке ожидающих он «extra»
        cfile = _plant_pyc(eng, "aurora_common.py", hashed=True)
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == SCRIPTS / "aurora_stats.py", \
            "подложенный hash-pyc не запер папку движка проекта"
        pend = B.pending_scripts(p)
        assert [s["state"] for s in pend] == ["extra"] and pend[0]["script"].endswith(cfile.name), pend
        cfile.unlink()
        assert Path(ck.script_path(str(p), "aurora_stats.py")) == eng / "aurora_stats.py"
        # 2. байт-код по времени, подогнанный под исходник: проверка его не отличит,
        # но запуск с префиксом машины его не читает
        _plant_pyc(eng, "aurora_common.py", hashed=False)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPYCACHEPREFIX"}
        import subprocess
        run1 = lambda e: subprocess.run([sys.executable, str(eng / "aurora_stats.py"), "--help"],  # noqa: E731
                                        cwd=p, env=e, capture_output=True, text=True, timeout=120)
        run1(env)
        assert mk.hit(), "контроль: подогнанный pyc не сработал без префикса — проверка ничего не доказывает"
        mk.reset()
        run1({**env, "PYTHONPYCACHEPREFIX": AC.pycache_prefix()})
        assert not mk.hit(), "подложенный pyc исполнился при запуске с префиксом байт-кода машины"
        # процесс движка закрепляет префикс сам: потомки наследуют его из окружения
        assert os.environ.get("PYTHONPYCACHEPREFIX") and sys.pycache_prefix
        assert "pycache_prefix=" in " ".join(AC.pycache_args())
    finally:
        mk.close()
        restore()


@test
def test_report_steps_and_graph_library_come_from_the_kit_not_the_project(tmp: Path):
    """Шаги отчёта (`.aurora/reports/analyst`) и библиотека графа (`.aurora/vendor`) лежат вне
    `.aurora/scripts`, и согласие их не сверяет — берутся китовые; зонд панели не пускает папку
    проекта в `sys.path`."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    G = importlib.import_module("kb_graph")
    restore = _own_consent(tmp)
    mk = _Marker(tmp)
    try:
        p = _project(tmp)
        pkg = p / ".aurora" / "reports" / "analyst"
        shutil.copytree(KIT / "reports" / "analyst", pkg, ignore=shutil.ignore_patterns("__pycache__"))
        with open(pkg / "paths.py", "a", encoding="utf-8") as f:
            f.write("\nopen(os.environ['AURORA_PWN_MARKER'], 'a').write('paths')\n")
        _evil(p / "json.py")                       # подмена stdlib в корне проекта (cwd зонда)
        ck.report_state(str(p))
        assert not mk.hit(), "зонд панели исполнил код из папки проекта"
        # report_analyst из проекта берёт шаги кита
        src = (SCRIPTS / "report_analyst.py").read_text(encoding="utf-8")
        assert "kit_root()" in src and "reports\", \"analyst" in src
        vend = p / ".aurora" / "vendor"
        vend.mkdir(parents=True)
        (vend / "cytoscape.min.js").write_text("/* FOREIGN */", encoding="utf-8")
        assert "FOREIGN" not in G._cytoscape_js()
    finally:
        mk.close()
        restore()


@test
def test_a_nested_script_name_is_compared_with_the_right_kit_folder(tmp: Path):
    """Вложенное имя (`agents/x.py`): эталон — `scripts/` кита, а не `scripts/agents/`; иначе
    каждый файл папки считался новым, и чистая копия кита запиралась."""
    _path()
    AC = importlib.import_module("aurora_common")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        eng = p / ".aurora" / "scripts"
        shutil.copytree(SCRIPTS, eng, ignore=shutil.ignore_patterns("__pycache__"))
        nested = next(SCRIPTS.glob("agents/*.py")).relative_to(SCRIPTS).as_posix()
        info = AC.script_consent(p, nested, str(SCRIPTS / nested))
        assert info["state"] == "same" and not info["blocked_by"], info
        assert Path(info["reference"]) == SCRIPTS
        assert AC.trusted_engine_script(p, nested, str(SCRIPTS / nested)) == str(eng / nested)
        # запасной скрипт из самого проекта: возвращается китовый вложенный файл
        (eng / nested).write_text("x = 1\n", encoding="utf-8")
        assert AC.trusted_engine_script(p, nested, str(eng / nested)) == str(SCRIPTS / nested)
    finally:
        restore()


@test
def test_approval_without_a_shown_hash_is_refused_and_extras_are_reported(tmp: Path):
    """Панельный запрос согласия без хеша не одобряет «то, что лежит сейчас»; команда
    `--approve --scripts` не говорит «согласовано» молча, пока папку держит файл, которому
    согласие не выдать. Окно «Одобрить все» знает про неполный итог."""
    _path()
    import io
    from contextlib import redirect_stdout
    B = importlib.import_module("bots")
    ck = importlib.import_module("aurora_cockpit")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        rel = _git_bot(B, p)
        assert ck.bots_action("approve", str(p), {"file": rel}) == {"ok": False, "error": "changed"}
        assert B.list_bots(p)[0]["consent"] == "new"
        digest = B.consent_view(p, rel)["sha256"]
        assert ck.bots_action("approve", str(p), {"file": rel, "sha256": digest})["ok"]
        eng = p / ".aurora" / "scripts"
        eng.mkdir(parents=True)
        shutil.copy(SCRIPTS / "aurora_stats.py", eng / "aurora_stats.py")
        _evil(eng / "aurora_common.py")
        assert ck.bots_action("approve", str(p), {"script": "aurora_common.py"})["error"] == "changed"
        (eng / "extra_mod.pyc").write_bytes(b"\0\0")
        ns = type("A", (), {"all": False, "scripts": True, "script": "", "bot": "", "json": False})()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = B._approve_cli(str(p), ns)
        assert rc == 1 and "extra_mod.pyc" in buf.getvalue(), buf.getvalue()
        view = (COCKPIT / "modules/bots/view.js").read_text(encoding="utf-8")
        assert "bots.consent.partial" in view
        for lang in ("ru", "en"):
            tx = json.loads((COCKPIT / f"modules/bots/i18n/{lang}.json").read_text(encoding="utf-8"))
            assert tx.get("bots.consent.partial"), lang
    finally:
        restore()


# ------------------------------------------------- доработка 3: сервер Авроры, реестр потомков, aurora.py

@test
def test_the_coach_knows_the_aurora_server_behind_interpreter_flags(tmp: Path):
    """Настоящая конфигурация MCP ставит перед скриптом флаги интерпретатора (`-X pycache_prefix=…`):
    «Улучшить с ИИ» узнаёт сервер Авроры по скрипту среди всех аргументов и берёт его список
    инструментов без запуска процесса, а не отправляет сервер на опрос."""
    _path()
    import agent_core as AG
    import aurora_mcp as M
    import bot_coach as BCo
    p = _project(tmp)
    args = AG.mcp_config(str(p), with_aurora=True)["mcpServers"][AG.AURORA_MCP]["args"]
    assert args[0] == "-X" and any(str(a).endswith("aurora_mcp.py") for a in args), args
    calls = []

    def probe(project, servers=None, describe=False, timeout=0):
        calls.append(sorted(servers or {}))
        return {"ok": True, "servers": {}}
    inv = BCo.inventory(p, {"mcp": [AG.AURORA_MCP]}, probe=probe)
    assert calls == [], f"сервер Авроры ушёл на опрос: {calls}"
    assert [t["name"] for t in inv["selected"][AG.AURORA_MCP]["tools"]] == [t["name"] for t in M.TOOLS]


@test
def test_children_of_the_panel_see_the_owners_consent_registry_not_the_project_s(tmp: Path):
    """Файл настроек проекта приезжает через git: `AURORA_CONSENT_HOME` из него не доезжает до
    потомков, а реестр потомков всегда тот же, что у родителя. Иначе подложенная папка с готовым
    реестром «согласила» бы чужого бота."""
    _path()
    import subprocess
    AC = importlib.import_module("aurora_common")
    B = importlib.import_module("bots")
    restore = _own_consent(tmp)
    try:
        p = _project(tmp)
        rel = _git_bot(B, p)
        owner_home = os.environ["AURORA_CONSENT_HOME"]
        # реестр злоумышленника, в котором бот уже одобрен
        attacker = tmp / "attacker-home"
        os.environ["AURORA_CONSENT_HOME"] = str(attacker)
        assert B.approve(p, rel, expect=B.consent_view(p, rel)["sha256"])["ok"]
        os.environ["AURORA_CONSENT_HOME"] = owner_home
        assert (attacker / "consent.json").is_file() and B.list_bots(p)[0]["consent"] == "new"
        (p / AC.ENV_FILE).write_text(f"AURORA_CONSENT_HOME={attacker}\nAURORA_OTHER=1\n", encoding="utf-8")
        env = AC.child_env(str(p))
        assert env["AURORA_CONSENT_HOME"] == owner_home, env["AURORA_CONSENT_HOME"]
        assert env.get("AURORA_OTHER") == "1", "остальные настройки проекта доезжать обязаны"
        cp = subprocess.run([sys.executable, str(SCRIPTS / "bots.py"), "--check", "--bot", rel,
                             "--project", str(p)], capture_output=True, text=True, encoding="utf-8",
                            env=env, timeout=120)
        assert "ждёт согласия" in cp.stdout, cp.stdout + cp.stderr
        # без заданной переменной потомок всё равно получает путь реестра родителя
        del os.environ["AURORA_CONSENT_HOME"]
        try:
            assert AC.child_env(str(p))["AURORA_CONSENT_HOME"] == AC.consent_home()
        finally:
            os.environ["AURORA_CONSENT_HOME"] = owner_home
    finally:
        restore()


@test
def test_the_terminal_launcher_does_not_run_a_foreign_project_engine(tmp: Path):
    """`aurora.py <команда> <клон>` и `aurora.py setup <клон>` идут через ту же проверку согласия,
    что и панель: подложенный `.aurora/scripts/aurora_doctor.py` не исполняется, работает скрипт
    кита; копия кита проходит."""
    _path()
    import subprocess
    spec = importlib.util.spec_from_file_location("aurora_launcher", KIT / "aurora.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    restore = _own_consent(tmp)
    mk = _Marker(tmp)
    try:
        p = _project(tmp)
        eng = p / ".aurora" / "scripts"
        for name in ("aurora_doctor.py", "aurora_setup.py"):
            _evil(eng / name)
        subprocess.run([sys.executable, str(KIT / "aurora.py"), "doctor", str(p)], cwd=tmp,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        assert not mk.hit(), "aurora.py исполнил подложенный скрипт клона"
        assert launcher.engine_script(p, "aurora_setup.py") == str(SCRIPTS / "aurora_setup.py")
        assert launcher.engine_script(p, "aurora_doctor.py") == str(SCRIPTS / "aurora_doctor.py")
        _run_script(eng / "aurora_doctor.py", p)
        assert mk.hit(), "проверка не ловит подмену: подложенный скрипт не сработал даже напрямую"
        # чистая копия кита исполняется из проекта, как и прежде
        shutil.rmtree(eng)
        shutil.copytree(SCRIPTS, eng, ignore=shutil.ignore_patterns("__pycache__"))
        assert launcher.engine_script(p, "aurora_setup.py") == str(eng / "aurora_setup.py")
        src = (KIT / "aurora.py").read_text(encoding="utf-8")
        assert src.count("engine_script(") >= 4 and '".aurora/scripts" /' not in src
    finally:
        mk.close()
        restore()


@test
def test_the_venv_interpreter_probes_run_isolated(tmp: Path):
    """`python -c` берёт первым в sys.path текущую папку, а панель зовёт venv из папки проекта:
    `zipfile.py` или `email/` в корне клона исполнились бы без согласия владельца (ADR-026).
    Три зонда venv — версия пакета, версия Python, проверка Pydantic AI — идут с `-I`.
    Всё, что умеет стирать venv (`install` при старом Python), смотрит на каталог во `tmp`:
    настоящий `~/.aurora/venv` владельца проверка не трогает."""
    _path()
    import subprocess
    AG = importlib.import_module("agent_core")
    EX = importlib.import_module("aurora_extras")
    extra = "pydantic-ai"
    fake = tmp / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    elsewhere = tmp / "never-created"          # сюда смотрит rmtree в install(); файла fake он не заденет
    seen = []

    def spy(cmd, *a, **kw):
        seen.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    real_venv = EX.EXTRAS[extra]["venv"]
    assert not str(elsewhere).startswith(str(real_venv)), "подмена каталога venv указывает на настоящий"
    saved = (AG.VENV, AG.subprocess.run, EX.venv_python, EX.EXTRAS[extra]["venv"])
    AG.VENV = fake.parent.parent
    AG.subprocess.run = spy
    EX.venv_python = lambda extra_id: fake
    EX.EXTRAS[extra]["venv"] = elsewhere
    try:
        AG.venv_status()
        EX.installed_version(extra)
        EX.install(extra)
    finally:
        AG.VENV, AG.subprocess.run, EX.venv_python, EX.EXTRAS[extra]["venv"] = saved
    assert EX.EXTRAS[extra]["venv"] == real_venv
    probes = [c for c in seen if "-c" in c]
    assert len(probes) >= 3, f"не все зонды venv замечены: {probes}"
    for c in probes:
        assert "-I" in c and c.index("-I") < c.index("-c"), f"зонд venv без изоляции: {c}"
