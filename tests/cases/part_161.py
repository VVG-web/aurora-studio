"""Проверки движка Aurora, часть 161. Каркас и помощники — tests/harness.py.

«Установка: кому доступно» в работе: окружение процессов venv без PYTHONHOME/PYTHONPATH, настоящий запуск
адаптера с подставным «python venv» (заглушка говорит «готов» или падает), ответ панели для «Установки»,
запись после установки, полоса тревоги и итог прогона без имён пользователей. venv, общая папка машины и
HOME — только временные; сети и модели нет.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test


def _mod(name: str):
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module(name)


def _stub(tmp: Path, body: str, name: str = "pydantic_ai_adapter.py") -> Path:
    path = tmp / "engine" / "scripts" / "agents" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class _Env:
    """Переменные окружения процесса на время проверки (None — снять)."""

    def __init__(self, **kw):
        self.kw, self.saved = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Заглушка адаптера: шум библиотеки, затем приветствие с тем, что процесс видит в окружении; дочитав
# трубу, выходит — как настоящий адаптер.
READY = """
    import json, os, sys
    print("баннер библиотеки, не JSON", flush=True)
    seen = "PP=" + os.environ.get("PYTHONPATH", "-") + ";PH=" + os.environ.get("PYTHONHOME", "-") + \\
           ";U8=" + os.environ.get("PYTHONUTF8", "-") + ";CWD=" + os.path.basename(os.getcwd())
    print(json.dumps({"ready": True, "version": seen}), flush=True)
    sys.stdin.read()
"""


@test
def test_venv_processes_get_no_pythonhome_or_pythonpath_but_keep_utf8(tmp: Path):
    """PYTHONHOME/PYTHONPATH чужого Python окружения в процессы venv не попадают (адаптер, pip и зонды
    надстроек, graphify), а PYTHONUTF8 и PYTHONIOENCODING остаются — поэтому не `-E`. Панель называет
    только имена заданных переменных, без значений."""
    C = _mod("aurora_common")
    EX = _mod("aurora_extras")
    with _Env(PYTHONPATH="/srv/corp/lib", PYTHONHOME="/srv/corp/python", PYTHONUTF8=None, PYTHONIOENCODING=None):
        env = C.venv_env()
        assert "PYTHONPATH" not in env and "PYTHONHOME" not in env, sorted(k for k in env if k.startswith("PYTHON"))
        assert env.get("PYTHONUTF8") == "1" and env.get("PYTHONIOENCODING") == "utf-8", env
        assert C.python_env_set() == ["PYTHONHOME", "PYTHONPATH"]
        assert C.venv_env({"pythonpath": "x", "A": "1"}, B="2") == {"A": "1", "B": "2"}   # регистр Windows
        seen = {}

        def spy(cmd, *a, **kw):
            seen.setdefault("env", kw.get("env"))
            return subprocess.CompletedProcess(cmd, 1, "", "")
        saved = EX.subprocess.run
        EX.subprocess.run = spy
        try:
            EX._run(["python", "-c", "pass"])
        finally:
            EX.subprocess.run = saved
        assert seen["env"] is not None and "PYTHONPATH" not in seen["env"] and "PYTHONHOME" not in seen["env"], seen
        # graphify: тот же запуск, что у движка (`graphify_adapter._call`)
        GA = _mod("agents.graphify_adapter")
        got = {}

        def spy_ga(cmd, *a, **kw):
            got["env"] = kw.get("env")
            return subprocess.CompletedProcess(cmd, 0, "{}\n", "")
        saved = (GA.python, GA.subprocess.run)
        GA.python, GA.subprocess.run = (lambda: sys.executable), spy_ga
        try:
            GA._call("print(1)", {})
        finally:
            GA.python, GA.subprocess.run = saved
        assert "PYTHONPATH" not in got["env"] and got["env"].get("GRAPHIFY_OUT"), got
    assert C.python_env_set({"PATH": "/bin"}) == []


@test
def test_real_launch_reads_ready_with_the_runs_argv_env_and_folder(tmp: Path):
    """Настоящий запуск адаптера (`adapter_probe`): тот же argv, что у прогона, окружение без PYTHONPATH и
    папка проекта; «готов» после постороннего баннера — работает. Заглушка, которая падает, не ставит пакет
    или молчит, даёт точную причину из таблицы `CAUSES`; нет питона venv или файла адаптера в движке
    проекта — свои причины, без запуска."""
    AG = _mod("agent_core")
    restore = set_home(tmp / "home")
    saved = (AG.VENV, AG._venv_python, AG._base_problem)
    project = tmp / "PRJ-A"
    project.mkdir()
    try:
        AG.VENV = tmp / "home" / ".aurora" / "venv"
        AG._venv_python = lambda: Path(sys.executable)       # подставной «python venv»
        AG._base_problem = lambda: ""
        with _Env(PYTHONPATH=str(tmp / "corp-lib")):
            ok = AG.adapter_probe(_stub(tmp, READY), str(project), wait=30)
        assert ok["ok"] is True and ok["kind"] == "" and ok["why"] == "", ok
        assert "PP=-;PH=-;U8=1;CWD=PRJ-A" == ok["version"], ok
        died = AG.adapter_probe(_stub(tmp, "import sys\nsys.exit(3)\n"), wait=30)
        assert (died["ok"], died["kind"], died["sub"], died["fix"]) == (False, "process_died", "start", "install"), died
        assert "(код 3)" in died["why"], died
        missing = AG.adapter_probe(_stub(tmp, """
            import json
            print(json.dumps({"ready": False, "error": "pydantic-ai не импортируется: ModuleNotFoundError",
                              "kind": "import_failed", "sub": "missing", "module": "pydantic_ai"}), flush=True)
        """), wait=30)
        assert (missing["kind"], missing["sub"]) == ("import_failed", "missing") and "pydantic_ai" in missing["why"], missing
        hung = AG.adapter_probe(_stub(tmp, "import time\ntime.sleep(20)\n"), wait=0.5)
        assert (hung["kind"], hung["sub"]) == ("process_hang", "start"), hung
        # движок проекта без файла адаптера — «Версия», без запуска
        gone = AG.adapter_probe(project / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py")
        assert (gone["kind"], gone["sub"], gone["fix"]) == ("adapter_file", "project", "version"), gone
        # нет питона venv — «Установка»
        AG._venv_python = lambda: AG.VENV / "bin" / "python"
        none = AG.adapter_probe(_stub(tmp, READY))
        assert (none["kind"], none["fix"]) == ("venv", "install") and "~/.aurora/venv" in none["why"], none
    finally:
        AG.VENV, AG._venv_python, AG._base_problem = saved
        restore()


@test
def test_install_page_answer_has_who_rights_run_and_other_users(tmp: Path):
    """Ответ панели для «Установки» (`/api/install/access`): кто проверен и с какими правами, какие переменные
    снимаются, и по каждой надстройке — настоящий запуск и чужие записи. Ответ держится в памяти панели до
    «Проверить снова» или установки; адаптер берётся из той копии движка, которой пойдёт прогон проекта.
    Полоса тревоги venv называет пользователя панели; строки «Установки» — `--user` и winget для пользователя."""
    ck = _mod("aurora_cockpit")
    AC = _mod("access_check")
    AG = _mod("agent_core")
    EX = _mod("aurora_extras")
    calls = []
    run_ok = {"ok": True, "version": "9.9.9", "kind": "", "sub": "", "fix": "", "why": ""}
    saved = (AG.adapter_probe, EX.run_probe, EX.install, ck.CACHE.pop("access", None), ck.CACHE.pop("me", None))
    AG.adapter_probe = lambda adapter=None, cwd=None, wait=0: calls.append((adapter, cwd)) or dict(run_ok)
    EX.run_probe = lambda eid, timeout=0: {"ok": False, "version": "", "kind": "venv", "sub": "", "fix": "install",
                                           "why": "venv graphify не найден — «Установка» → graphify"}
    EX.install = lambda eid, q=None: {"ok": True, "version": "9.9.9", "log": ""}
    try:
        with _Env(AURORA_SHARED_DIR=str(tmp / "machine")):
            AC.record_install("graphify", "0.9.1", "/home/admin1/.aurora/graphify",
                              me={"user": "admin1", "elevated": True})
            a = ck.install_access("")
            assert set(a) == {"me", "python_env", "components", "at"}, sorted(a)
            assert set(a["me"]) == {"user", "home", "elevated", "session_user", "foreign"}, a["me"]
            assert isinstance(a["python_env"], list) and set(a["components"]) == {"pydantic-ai", "graphify"}
            for c in a["components"].values():
                assert set(c) == {"title", "venv", "run", "owner", "note", "others"}, sorted(c)
                assert set(c["run"]) == {"ok", "version", "kind", "sub", "fix", "why"}, c["run"]
            assert a["components"]["pydantic-ai"]["others"] == [], "работает для вас — чужие записи не нужны"
            other = a["components"]["graphify"]["others"]
            assert [o["user"] for o in other] == ["admin1"] and other[0]["elevated"] is True, other
            assert calls == [(ck.run_adapter(""), None)], calls
            assert ck.run_adapter("").endswith(os.path.join("scripts", "agents", "pydantic_ai_adapter.py"))
            ck.install_access("")
            assert len(calls) == 1, "ответ не взят из памяти панели"
            ck.install_access("", fresh=True)
            assert len(calls) == 2, "«Проверить снова» не запустило заново"
            ck.extras_install("pydantic-ai")
            ck.install_access("")
            assert len(calls) == 3, "после установки запуск не проверен заново"
            json.dumps(a, ensure_ascii=False)
        # проект со своим движком: адаптер — его копии
        prj = tmp / "PRJ-B"
        (prj / ".aurora" / "scripts" / "agents").mkdir(parents=True)
        (prj / ".aurora" / "scripts" / "agent_core.py").write_text("", encoding="utf-8")
        assert ck.run_adapter(str(prj)) == str(prj / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py")
    finally:
        AG.adapter_probe, EX.run_probe, EX.install = saved[:3]
        ck.CACHE.pop("access", None)
        if saved[4] is not None:
            ck.CACHE["me"] = saved[4]
    # тревога: беда venv — с пользователем панели, беда связи — без
    rows_saved = AG.read_alarm
    AG.read_alarm = lambda: {"p1": {**AG.Why("venv", path="~/.aurora/venv/bin/python").row(), "path": "p1",
                                    "at": "", "calls": 1},
                             "link:1": {**AG.Why("dns", n=1, model="m").row(), "path": "link:1", "at": "", "calls": 3}}
    try:
        rows = {r["path"]: r for r in ck.adapter_alarm()}
    finally:
        AG.read_alarm = rows_saved
    assert rows["p1"].get("user") == ck.me().get("user") and "user" not in rows["link:1"], rows
    # строки «Установки»: пакеты с `--user`, если папка пакетов закрыта; git и pandoc — winget для пользователя
    env = ck.environment(fresh=True)
    for i in env["items"]:
        if i.get("pip"):
            assert isinstance(i["user"], bool) and (("--user " in i["install"]) == i["user"]), i
        if i["name"] in ("git", "pandoc"):
            assert (i["install_user"].endswith("--scope user") if ck.platform_key() == "nt"
                    else i["install_user"] == ""), i
    src = (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    view = (KIT / "cockpit" / "modules" / "install" / "view.js").read_text(encoding="utf-8")
    assert '"/api/install/access"' in src and '"/api/install/access"' in view and "function accessCard" in view
    assert "confirm(" not in view.split("function accessCard")[1].split("function harnessCard")[0], \
        "«Установка: кому доступно» без диалогов согласия"


@test
def test_successful_install_leaves_a_record_for_other_users(tmp: Path):
    """После успешной установки — надстройки (`aurora_extras.install`) и пакета Python из панели
    (`/api/env/install`) — в общей папке машины появляется запись этого пользователя; venv и HOME — временные."""
    EX = _mod("aurora_extras")
    ck = _mod("aurora_cockpit")
    AC = _mod("access_check")
    eid = "pydantic-ai"
    venv = tmp / "venv"
    vpy = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    vpy.parent.mkdir(parents=True)
    vpy.write_text("", encoding="utf-8")
    real_venv = EX.EXTRAS[eid]["venv"]
    assert not str(venv).startswith(str(real_venv)), "подмена venv указывает на настоящий"
    names = ("_run", "installed_version", "plan_lock", "_install_lock", "_upgrade_pip", "check_compat",
             "save_lock", "_pip_older_than", "lock_files", "aged_version")
    saved = {n: getattr(EX, n) for n in names}
    ok = subprocess.CompletedProcess([], 0, "3.12", "")
    EX.EXTRAS[eid]["venv"] = venv
    EX._run = lambda cmd, timeout=60: ok
    EX.installed_version = lambda e: "9.9.9"
    EX.plan_lock = lambda *a, **k: ("# lock\n", "")
    EX._install_lock = lambda vpy, path: ok
    EX._upgrade_pip = lambda *a, **k: None
    EX.check_compat = lambda v: {"ok": True, "problems": []}
    EX.save_lock = lambda *a, **k: tmp / "lock.txt"
    EX._pip_older_than = lambda *a, **k: False
    EX.lock_files = lambda e: []
    EX.aged_version = lambda dist, days, now=0.0: "5.0.0"          # без сети: выпуск старше паузы
    try:
        with _Env(AURORA_SHARED_DIR=str(tmp / "machine")):
            res = EX.install(eid)
            assert res["ok"], res
            me = AC.whoami()
            mine = json.loads((tmp / "machine" / "installs" / (AC._file_name(me["user"]) + ".json"))
                              .read_text(encoding="utf-8"))
            row = mine["components"][eid]
            assert (row["version"], row["path"], row["user"]) == ("9.9.9", str(venv), me["user"]), row
            # пакет Python из «Установки» — тот же журнал
            real = (ck.subprocess.run, ck.environment)
            ck.subprocess.run = lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, "ok", "")
            ck.environment = lambda fresh=False: {"items": [{"pip": "lxml", "ok": True}]}
            try:
                out = ck.env_install("lxml")
            finally:
                ck.subprocess.run, ck.environment = real
            assert out["ok"], out
            mine = json.loads((tmp / "machine" / "installs" / (AC._file_name(me["user"]) + ".json"))
                              .read_text(encoding="utf-8"))
            assert set(mine["components"]) == {eid, "lxml"}, mine
        # общая папка недоступна — строка в журнале установки, установка не падает
        blocker = tmp / "file-instead-of-folder"
        blocker.write_text("x", encoding="utf-8")
        with _Env(AURORA_SHARED_DIR=str(blocker)):
            res = EX.install(eid)
        assert res["ok"] and "запись об установке не сохранена" in res["log"], res
    finally:
        EX.EXTRAS[eid]["venv"] = real_venv
        for n, v in saved.items():
            setattr(EX, n, v)


@test
def test_run_summary_says_another_user_has_it_without_naming_anyone(tmp: Path):
    """Итог прогона, журнал сбоев (`failures.jsonl`) и файл тревоги про беду venv говорят «у другого
    пользователя этой машины есть установка (запись от <дата>)» — без имени и пути (правило `_shown_path`);
    английский текст — по той же таблице. Имя называет только «Установка»."""
    AG = _mod("agent_core")
    AC = _mod("access_check")
    ck = _mod("aurora_cockpit")
    restore = set_home(tmp / "home")
    run_dir = tmp / "run"
    run_dir.mkdir()
    saved = (dict(AG._OTHERS_SEEN), dict(AG.USAGE), set(AG._BYPASS_SAID))
    AG._OTHERS_SEEN.clear()
    try:
        with _Env(AURORA_SHARED_DIR=str(tmp / "machine"), AURORA_RUN_DIR=str(run_dir)):
            AC.record_install("pydantic-ai", "2.57.0", "C:\\Users\\admin1\\.aurora\\venv",
                              me={"user": "CORP\\admin1", "elevated": True})
            date = AC.others_date("pydantic-ai")
            assert len(date) == 10, date
            why = AG._note_others(AG.Why("venv", path="~/.aurora/venv/bin/python"))
            assert str(why).endswith(f"; у другого пользователя этой машины есть установка (запись от {date})"), why
            assert why.en.endswith(f"; another user of this machine has an installation (record from {date})"), why.en
            assert ck.translate_message(str(why), "en") == why.en, ck.translate_message(str(why), "en")
            assert AG._note_others(AG.Why("dns", n=1, model="m")).args.get("others") is None, "беда связи — не про venv"
            plain = AG._note_others(AG.Why.raw("venv python: x не найден", "venv"))
            assert plain.plain and "у другого пользователя" in str(plain) and plain.kind == "venv"
            back = AG.alarm_cause(why.row())
            assert str(back) == str(why), "из файла тревоги причина читается с той же припиской"
            AG.note_bypass(why)
            texts = [AG.USAGE["bypass_why"], (run_dir / AG.FAIL_FILE).read_text(encoding="utf-8"),
                     AG.alarm_path().read_text(encoding="utf-8")]
            for text in texts:
                assert "у другого пользователя этой машины есть установка" in text, text
                assert "admin1" not in text and "C:\\\\Users" not in text and "C:\\Users" not in text, text
                assert str(Path.home()) not in text, text
    finally:
        AG._OTHERS_SEEN.clear()
        AG._OTHERS_SEEN.update(saved[0])
        AG.USAGE.clear()
        AG.USAGE.update(saved[1])
        AG._BYPASS_SAID.clear()
        AG._BYPASS_SAID.update(saved[2])
        restore()


@test
def test_venv_status_and_doctor_print_who_rights_real_launch_and_other_users(tmp: Path):
    """`agent_core.py --venv-status` и `kit:doctor` печатают то же, что «Установка»: для кого проверено и с какими
    правами, настоящий запуск и чужие записи; `--json` несёт это полем `access`. Запуск — в своём HOME и со
    своей общей папкой машины."""
    home = tmp / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "AURORA_SHARED_DIR": str(tmp / "machine")}
    AC = _mod("access_check")
    with _Env(AURORA_SHARED_DIR=str(tmp / "machine")):
        AC.record_install("pydantic-ai", "2.57.0", "/home/admin1/.aurora/venv", me={"user": "admin1", "elevated": False})
    cp = subprocess.run([sys.executable, str(SCRIPTS / "agent_core.py"), "--venv-status"], cwd=str(tmp),
                        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    out = cp.stdout
    assert cp.returncode == 0, out + cp.stderr
    assert "Проверено для: " in out and "права: " in out, out
    assert "Pydantic AI: для вас не запускается — venv python:" in out, out
    assert "Pydantic AI 2.57.0 стоит у пользователя admin1" in out and "каждый пользователь ставит его себе" in out, out
    cp = subprocess.run([sys.executable, str(SCRIPTS / "agent_core.py"), "--venv-status", "--json"], cwd=str(tmp),
                        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    d = json.loads(cp.stdout.strip().splitlines()[-1])
    assert {"ok", "version", "path", "access"} <= set(d) and d["access"]["components"]["pydantic-ai"]["others"], d
    doctor = (SCRIPTS / "aurora_doctor.py").read_text(encoding="utf-8")
    assert "_ac.report_lines(_ac.report())" in doctor, "доктор не печатает того же, что «Установка»"
