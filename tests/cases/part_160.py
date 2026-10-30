"""Проверки движка Aurora, часть 160. Каркас и помощники — tests/harness.py.

«Установка: кому доступно» (`scripts/access_check.py`): кто я (пользователь процесса, права, пользователь
сеанса — с подменой ветвей Windows и POSIX), записи «кто что поставил» в общей папке машины, основа venv из
чужого профиля и чужой venv. Общая папка — только временная (`AURORA_SHARED_DIR`), настоящие ProgramData,
/Users/Shared и HOME проверки не трогают; сети и модели нет.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from harness import SCRIPTS, set_home, test


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


class _Patch:
    """Подменить атрибуты модуля на время проверки и вернуть прежние."""

    def __init__(self, mod, **kw):
        self.mod, self.kw, self.saved = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self.mod

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)


class _Shared:
    """Своя общая папка машины на время проверки (`AURORA_SHARED_DIR`)."""

    def __init__(self, path: Path):
        self.path, self.saved = path, None

    def __enter__(self):
        self.saved = os.environ.get("AURORA_SHARED_DIR")
        os.environ["AURORA_SHARED_DIR"] = str(self.path)
        return self.path

    def __exit__(self, *exc):
        if self.saved is None:
            os.environ.pop("AURORA_SHARED_DIR", None)
        else:
            os.environ["AURORA_SHARED_DIR"] = self.saved


@test
def test_whoami_tells_process_user_rights_and_session_user_apart(_t):
    """Кто я: Windows — пользователь процесса с доменом, права администратора, пользователь сеанса; POSIX —
    root через sudo и тот, кто его позвал. Панель от другой учётной записи или с правами администратора
    видна по `foreign`/`elevated`; тот же пользователь в другом регистре — не чужой; имя входа root у
    процесса, запущенного службой, — не «вошёл другой»."""
    AC = _mod("access_check")
    win = {"USERNAME": "admin1", "USERDOMAIN": "CORP"}
    with _Patch(AC, _win_admin=lambda: True, _win_session_user=lambda: "CORP\\user1"):
        me = AC.whoami(nt=True, env=win)
    assert me["user"] == "CORP\\admin1" and me["elevated"] is True, me
    assert me["session_user"] == "CORP\\user1" and me["foreign"] is True, me
    assert me["home"] == str(Path.home()), me
    with _Patch(AC, _win_admin=lambda: False, _win_session_user=lambda: "corp\\USER1"):
        me = AC.whoami(nt=True, env={"USERNAME": "user1", "USERDOMAIN": "CORP"})
    assert me["foreign"] is False and me["elevated"] is False, me
    with _Patch(AC, _win_admin=lambda: None, _win_session_user=lambda: None):
        me = AC.whoami(nt=True, env={"USERNAME": "user1"})
    assert me["user"] == "user1" and me["elevated"] is None and me["foreign"] is False, me
    # POSIX: sudo — процесс от root, позвал user1
    with _Patch(AC, _posix_user=lambda: "root", _posix_root=lambda: True, _posix_login=lambda: "user1"):
        me = AC.whoami(nt=False, env={"SUDO_USER": "user1"})
    assert me == {**me, "user": "root", "elevated": True, "session_user": "user1", "foreign": True}, me
    # su под другим пользователем без sudo — имя входа остаётся прежним
    with _Patch(AC, _posix_user=lambda: "admin1", _posix_root=lambda: False, _posix_login=lambda: "user1"):
        me = AC.whoami(nt=False, env={})
    assert me["foreign"] is True and me["session_user"] == "user1", me
    # служба от root со сброшенными правами: имя входа root — не чужой сеанс
    with _Patch(AC, _posix_user=lambda: "user1", _posix_root=lambda: False, _posix_login=lambda: "root"):
        me = AC.whoami(nt=False, env={})
    assert me["foreign"] is False and me["session_user"] is None, me
    assert AC.same_user("CORP\\user1", "user1") and not AC.same_user("CORP\\user1", "OTHER\\user1")
    assert AC.rights_text({"elevated": True}) == "администратор" and AC.rights_text({}) == "не определены"


@test
def test_windows_ctypes_branches_run_harmlessly_and_return_plain_types(_t):
    """Ветви ctypes (`IsUserAnAdmin`, `WTSQuerySessionInformationW`) вызываются на любой системе: на Windows
    они отвечают настоящими значениями, на остальных — None; сбой — None, а не исключение. Подставные
    библиотеки проверяют разбор ответа без Windows."""
    AC = _mod("access_check")
    admin, session = AC._win_admin(), AC._win_session_user()
    assert admin is None or isinstance(admin, bool), admin
    assert session is None or (isinstance(session, str) and session), session
    if os.name == "nt":
        assert isinstance(admin, bool), "на Windows права должны быть известны"
    else:
        assert admin is None and session is None, (admin, session)

    class Shell:
        def __init__(self, v):
            self.v = v

        def IsUserAnAdmin(self):
            if isinstance(self.v, Exception):
                raise self.v
            return self.v
    assert AC._win_admin(Shell(1)) is True and AC._win_admin(Shell(0)) is False
    assert AC._win_admin(Shell(OSError("нет функции"))) is None

    class Broken:                      # библиотека без нужной функции — None, а не AttributeError
        pass
    assert AC._win_session_user(Broken()) is None
    me = AC.whoami()
    assert set(me) == {"user", "home", "elevated", "session_user", "foreign"}, me
    assert isinstance(me["foreign"], bool) and (me["user"] is None or isinstance(me["user"], str)), me
    assert me["elevated"] is None or isinstance(me["elevated"], bool), me


@test
def test_install_records_are_one_file_per_user_and_name_other_users(tmp: Path):
    """Запись «кто что поставил»: файл свой у каждого пользователя (компонент, версия, путь, пользователь,
    права, время UTC), запись атомарная, второй компонент дописывается к первому. Чтение отдаёт только
    ДРУГИХ пользователей, свежие первыми; битый файл пропускается; не удалось записать — строка причины,
    а не исключение. На POSIX папки открыты всем на запись с липким битом, как /tmp."""
    AC = _mod("access_check")
    admin = {"user": "CORP\\admin1", "elevated": True}
    user = {"user": "CORP\\user1", "elevated": False}
    with _Shared(tmp / "machine") as shared:
        assert AC.installs_dir() == shared / "installs"
        assert AC.record_install("pydantic-ai", "2.57.0", "C:\\Users\\admin1\\.aurora\\venv", me=admin) == ""
        assert AC.record_install("graphify", "0.9.1", "C:\\Users\\admin1\\.aurora\\graphify", me=admin) == ""
        assert AC.record_install("pydantic-ai", "2.50.0", "/home/user1/.aurora/venv", me=user) == ""
        files = sorted(p.name for p in (shared / "installs").iterdir())
        assert files == ["CORP_admin1.json", "CORP_user1.json"], files     # без .tmp-остатков
        data = json.loads((shared / "installs" / "CORP_admin1.json").read_text(encoding="utf-8"))
        row = data["components"]["pydantic-ai"]
        assert set(data["components"]) == {"pydantic-ai", "graphify"}, data
        assert {k: row[k] for k in ("component", "version", "user", "elevated")} == \
            {"component": "pydantic-ai", "version": "2.57.0", "user": "CORP\\admin1", "elevated": True}, row
        assert row["path"].endswith("venv") and row["at"].endswith("Z") and len(row["at"]) == 20, row
        if os.name != "nt":
            assert (shared / "installs").stat().st_mode & 0o1777 == 0o1777, oct((shared / "installs").stat().st_mode)
        (shared / "installs" / "broken.json").write_text("{не json", encoding="utf-8")
        seen = AC.others("pydantic-ai", me=user)
        assert [r["user"] for r in seen] == ["CORP\\admin1"], seen
        assert AC.others("pydantic-ai", me=admin)[0]["user"] == "CORP\\user1"
        assert AC.others("lxml", me=user) == []
        line = AC.other_line(seen[0], "Pydantic AI", user)
        assert line.startswith("Pydantic AI 2.57.0 стоит у пользователя CORP\\admin1 (поставлен ") and \
            "с правами администратора) в C:\\Users\\admin1\\.aurora\\venv" in line and \
            "для вас (CORP\\user1) он недоступен: каждый пользователь ставит его себе" in line, line
    blocker = tmp / "not-a-folder"
    blocker.write_text("файл на месте папки", encoding="utf-8")
    with _Shared(blocker):
        said = AC.record_install("pydantic-ai", "2.57.0", "x", me=user)
        assert said.startswith("запись об установке не сохранена в общей папке машины"), said
        assert AC.others("pydantic-ai", me=user) == [] and AC.others_date("pydantic-ai") == ""


@test
def test_shared_folder_is_per_os_and_never_real_in_tests(_t):
    """Общая папка машины: ProgramData на Windows, /Users/Shared на macOS, /var/tmp на Linux; прогон проверок
    пишет только в свою (`AURORA_SHARED_DIR` из каркаса), настоящую не трогает."""
    AC = _mod("access_check")
    saved = os.environ.pop("AURORA_SHARED_DIR")
    try:
        assert AC.shared_dir(nt=False, platform="darwin") == Path("/Users/Shared/AuroraStudio")
        assert AC.shared_dir(nt=False, platform="linux") == Path("/var/tmp/aurora-studio")
        assert AC.shared_dir(nt=True).name == "AuroraStudio"
    finally:
        os.environ["AURORA_SHARED_DIR"] = saved
    own = os.environ.get("AURORA_TESTS_HOME") or ""
    assert own and str(AC.installs_dir()).startswith(own), AC.installs_dir()


@test
def test_venv_built_on_another_users_python_is_its_own_cause(tmp: Path):
    """`pyvenv.cfg` с `home` в профиле ДРУГОГО пользователя (Windows, macOS, Linux) — `other_user`; Python,
    которого нет, — `missing`; общий профиль и системные папки — не чужие. До запуска движок называет
    `venv_foreign_base` для пропавшей основы (на POSIX ссылка `bin/python` в таком venv битая, «не найден»
    увёл бы искать файл), а после неудачного запуска — и для основы из чужого профиля."""
    AC = _mod("access_check")
    AG = _mod("agent_core")
    venv = tmp / "home" / ".aurora" / "venv"
    venv.mkdir(parents=True)

    def base(where: str, mine: str) -> str:
        (venv / "pyvenv.cfg").write_text(f"home = {where}\ninclude-system-site-packages = false\n", encoding="utf-8")
        return AC.venv_base_problem(venv, home=mine)
    assert base("C:\\Users\\admin1\\AppData\\Local\\Programs\\Python\\Python312", "C:\\Users\\user1") == "other_user"
    assert base("/Users/admin1/.pyenv/versions/3.12.4/bin", "/Users/user1") == "other_user"
    assert base("/home/admin1/.local/share/uv/python/cpython-3.12/bin", "/home/user1") == "other_user"
    assert not AC.other_profile("C:\\Users\\Public\\Python312", "C:\\Users\\user1")
    assert not AC.other_profile("/Users/Shared/python/bin", "/Users/user1")
    assert not AC.other_profile("/opt/homebrew/opt/python@3.12/bin", "/Users/user1")
    assert not AC.other_profile("C:\\Users\\USER1\\AppData\\Local\\Programs\\Python", "C:\\Users\\user1")
    assert base(str(tmp / "no-such-python" / "bin"), str(tmp)) == "missing"
    assert AC.venv_base_problem(tmp / "no-venv") == ""          # venv нет — это другая причина
    real = Path(sys.executable).parent
    (venv / "pyvenv.cfg").write_text(f"home = {real}\n", encoding="utf-8")
    assert AC.venv_base_problem(venv, home=str(real.parent)) == ""
    # движок: пропавшая основа — причина ещё до запуска; основа из чужого профиля — после неудачи
    restore = set_home(tmp / "home")
    saved = (AG.VENV,)
    try:
        AG.VENV = venv
        (venv / "pyvenv.cfg").write_text(f"home = {tmp / 'gone' / 'bin'}\n", encoding="utf-8")
        gap = AG._venv_gap()
        assert gap.kind == "venv_foreign_base" and gap.fix == "install" and gap.area == "adapter", gap
        assert "~/.aurora/venv" in str(gap) and "admin1" not in str(gap) and "поставьте для себя" in str(gap), gap
        (venv / "pyvenv.cfg").write_text("home = /home/admin1/python/bin\n", encoding="utf-8")
        died = AG._start_cause(AG.Why("process_died", "start", code=101))
        assert died.kind == "venv_foreign_base", died
        hello = AG._start_cause(AG.Why("import_failed", "missing", module="pydantic_ai"))
        assert hello.kind == "import_failed", "venv поднялся и назвал пакет — основа ни при чём"
        (venv / "pyvenv.cfg").unlink()
        assert AG._start_cause(AG.Why("process_died", "start", code=1)).kind == "process_died"
    finally:
        AG.VENV = saved[0]
        restore()


@test
def test_venv_of_another_owner_works_but_cannot_be_updated(tmp: Path):
    """POSIX: venv другого владельца или закрытый на запись (ставили через sudo) — `venv_not_yours` с
    текстом «работает, но обновить без прав владельца нельзя»; свой — пусто. На Windows проверки нет."""
    AC = _mod("access_check")
    AG = _mod("agent_core")
    venv = tmp / "venv"
    venv.mkdir()
    assert AC.venv_owner_problem(tmp / "absent") == ""
    if os.name == "nt":
        assert AC.venv_owner_problem(venv) == ""
        return
    assert AC.venv_owner_problem(venv) == ""
    if os.geteuid() != 0:                    # root пишет и в закрытое — проверка «только чтение» без смысла
        os.chmod(venv, 0o555)
        try:
            assert AC.venv_owner_problem(venv) == "readonly"
        finally:
            os.chmod(venv, 0o755)
    real_stat = AC.os.stat

    class Foreign:
        st_uid = os.geteuid() + 1
    with _Patch(AC.os, stat=lambda p, *a, **k: Foreign() if str(p) == str(venv) else real_stat(p, *a, **k)):
        assert AC.venv_owner_problem(venv) == "other_uid"
    why = AG.Why("venv_not_yours", path="~/.aurora/venv")
    assert why.fix == "install" and "обновить без прав владельца нельзя" in str(why) and "without the owner" in why.en
