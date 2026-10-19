"""Проверки движка Aurora, часть 93. Каркас и помощники — tests/harness.py.

Закрепление и «карантин» зависимостей (BL-SEC-21): установка надстройки решает набор пакетов заранее,
отбрасывает выпуски моложе паузы, ставит ровно решённое по хешам и оставляет снимок, по которому venv
откатывается целиком; версии зависимостей движка закреплены в requirements.txt.
Сеть и pip не запускаются: `_run` и `_get_json` подменены, venv и снимки — во временных папках,
после каждой проверки модуль перезагружается. Настоящий `~/.aurora` не затрагивается.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

def HX(tag: str) -> str:
    """Короткая метка хеша в тесте → настоящий образ sha256 (отчёт pip проверяется на 64 шестнадцатеричных знака)."""
    return hashlib.sha256(tag.encode()).hexdigest()


NOW = 1_800_000_000.0                       # фиксированное «сейчас» проверок
DAY = 86400.0


def _stamp(days_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.123456Z", time.gmtime(NOW - days_ago * DAY))


class Pip:
    """Подмена `aurora_extras._run` и `_get_json`: разговор с pip и PyPI по сценарию, журнал вызовов."""

    def __init__(self, rounds: list, ages: dict, freeze: str = "", pip_version: str = "pip 25.2 from x (python 3.13)\n"):
        self.rounds, self.ages, self.freeze, self.pip_version = list(rounds), ages, freeze, pip_version
        self.calls, self.constraints, self.locks, self.json_urls = [], [], [], []
        self.have = ["2.51.0"]
        self.fail_venv = False
        self.fail_binary = False        # первое решение с --only-binary падает: у пакета нет колеса
        self.url = "https://files.example.org/{n}-{v}.whl"
        self.upload_json = None         # подмена ответа PyPI для выпуска

    def report(self, path: str, pkgs: list) -> None:
        items = [{"metadata": {"name": n, "version": v},
                  "download_info": {"url": self.url.format(n=n, v=v),
                                    "archive_info": {"hashes": {"sha256": HX(h) if re.fullmatch(r"h[a-z0-9]*", h) else h}}}}
                 for n, v, h in pkgs]
        Path(path).write_text(json.dumps({"version": "1", "install": items}), encoding="utf-8")

    def run(self, cmd, timeout=60):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        joined = " ".join(cmd)

        def done(rc=0, out="", err=""):
            return subprocess.CompletedProcess(cmd, rc, out, err)
        if "import sys" in joined:
            return done(0, "3.13\n")
        if cmd[-1] == "--version":
            return done(0, self.pip_version)
        if "freeze" in cmd:
            return done(0, self.freeze)
        if "--report" in cmd:
            if self.fail_binary and "--only-binary=:all:" in cmd:
                return done(1, "", "ERROR: No matching distribution found (no wheel)")
            if "-c" in cmd:
                self.constraints.append(Path(cmd[cmd.index("-c") + 1]).read_text(encoding="utf-8"))
            pkgs = self.rounds.pop(0) if self.rounds else None
            if pkgs is None:
                return done(0)                       # pip «забыл» написать отчёт
            self.report(cmd[cmd.index("--report") + 1], pkgs)
            return done(0)
        if "-r" in cmd and "--no-deps" in cmd:
            text = Path(cmd[cmd.index("-r") + 1]).read_text(encoding="utf-8")
            self.locks.append(text)
            pinned = re.search(r"(?m)^pydantic-ai==(\S+)", text)
            self.have[0] = pinned.group(1) if pinned else "9.0.0"        # что в снимке, то и стоит после
            return done(0)
        if "-m" in cmd and "venv" in cmd:
            if self.fail_venv:
                return done(1, "", "venv не создан")
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)        # новый venv на деле появляется
            return done(0)
        if any(c.startswith("pydantic-ai==") for c in cmd):
            self.have[0] = next(c for c in cmd if c.startswith("pydantic-ai==")).split("==")[1]
        return done(0)

    def get_json(self, url, timeout=15, limit=0):
        self.json_urls.append(url)
        m = re.match(r"https://pypi\.org/pypi/([^/]+)/([^/]+)/json$", url)
        if m:
            if self.upload_json is not None:
                return self.upload_json
            age = self.ages.get((m.group(1), m.group(2)))
            if age is None:
                import urllib.error
                raise urllib.error.HTTPError(url, 404, "нет", {}, None)
            return {"urls": [{"filename": "x.whl", "digests": {"sha256": "h-" + m.group(1)},
                              "upload_time_iso_8601": _stamp(age)}]}
        if url.endswith("/pip/json"):
            return {"releases": {"26.0": [{"upload_time_iso_8601": _stamp(40), "yanked": False}],
                                 "26.1": [{"upload_time_iso_8601": _stamp(2), "yanked": False}],
                                 "26.2rc1": [{"upload_time_iso_8601": _stamp(50), "yanked": False}],
                                 "25.9": [{"upload_time_iso_8601": _stamp(60), "yanked": True}]}}
        raise AssertionError(f"неожиданный запрос в сеть: {url}")


def _patched(tmp: Path, pip: Pip):
    """Модуль надстроек на временных путях; возвращает (EX, venv, чем_ставят)."""
    EX = importlib.import_module("aurora_extras")
    importlib.reload(EX)
    venv = tmp / "venv"
    fake = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    EX.LOCKS = tmp / "locks"
    EX._now = lambda: NOW
    EX.EXTRAS["pydantic-ai"]["venv"] = venv
    EX._run = pip.run
    EX._get_json = pip.get_json
    EX.venv_python = lambda _id: fake
    EX.installed_version = lambda _id: pip.have[0]
    EX.python_for = lambda _id: sys.executable
    EX.check_compat = lambda v: {"ok": v != "9.0.0", "version": v, "problems": [] if v != "9.0.0" else ["адаптер не принял"]}
    return EX, venv


@test
def test_extras_install_skips_young_releases_and_installs_by_hashes(tmp: Path):
    """Выпуск моложе паузы запрещается и набор решается заново; ставится ровно решённое, по хешам."""
    pip = Pip(rounds=[[("a", "1.0", "ha"), ("b", "2.0", "hb")],        # b 2.0 вышел два дня назад
                      [("a", "1.0", "ha"), ("b", "1.9", "hb19")]],
              ages={("a", "1.0"): 90, ("b", "2.0"): 2, ("b", "1.9"): 30})
    EX, _venv = _patched(tmp, pip)
    try:
        EX.check_compat = lambda v: {"ok": True, "version": v, "problems": []}
        got = EX.plan_lock(Path("python"), "a b", 7, NOW)
        assert got[1] == "", got
        assert "b==1.9" in got[0] and f"--hash=sha256:{HX('hb19')}" in got[0] and "b==2.0" not in got[0], got[0]
        assert pip.constraints == ["b<2.0\n"], pip.constraints
        # то же через install(): ставят из файла с хешами, без зависимостей, а снимок остаётся
        pip.rounds[:] = [[("a", "1.0", "ha"), ("b", "2.0", "hb")], [("a", "1.0", "ha"), ("b", "1.9", "hb19")]]
        pip.have[0] = "2.51.0"
        res = EX.install("pydantic-ai", 7)
        assert res["ok"] and res["version"] == "9.0.0", res
        install_cmd = next(c for c in pip.calls if "-r" in c and "--no-deps" in c)
        assert "--require-hashes" in install_cmd, install_cmd
        assert pip.locks and f"b==1.9 \\\n    --hash=sha256:{HX('hb19')}" in pip.locks[0], pip.locks
        saved = EX.lock_files("pydantic-ai")
        assert [p.name for p in saved if p.name.endswith("-install.txt")] and res["lock"] in [p.name for p in saved], res
        assert any(c[-2:] == ["--quiet", "pip==26.0"] or "pip==26.0" in c for c in pip.calls), \
            "pip обновлён не до выпуска старше паузы"
    finally:
        importlib.reload(EX)


@test
def test_extras_install_refuses_when_only_young_releases_exist(tmp: Path):
    """Старой замены нет — отказ с названием пакета, и ничего не ставится (на старом коде ставилось что вышло)."""
    young = [("a", "1.0", "ha"), ("b", "2.0", "hb")]
    pip = Pip(rounds=[young] * 6, ages={("a", "1.0"): 90, ("b", "2.0"): 1})
    EX, _venv = _patched(tmp, pip)
    try:
        text, why = EX.plan_lock(Path("python"), "a b", 7, NOW)
        assert text == "" and "моложе 7 сут." in why and "b 2.0" in why, (text, why)
        pip.rounds[:] = [young] * 6
        res = EX.install("pydantic-ai", 7)
        assert not res["ok"] and "не удалось собрать набор" in res["log"], res
        assert not [c for c in pip.calls if "--require-hashes" in c or ("-r" in c and "--no-deps" in c)], \
            "при отказе что-то всё же поставили"
    finally:
        importlib.reload(EX)


@test
def test_extras_install_fails_closed_without_a_hashed_set(tmp: Path):
    """Нет отчёта pip или в нём нет хеша — не ставим вслепую; сеть не трогаем, пока набор не решён."""
    pip = Pip(rounds=[], ages={})
    EX, _venv = _patched(tmp, pip)
    try:
        res = EX.install("pydantic-ai", 7)
        assert not res["ok"] and "набор не решён" in res["log"], res
        assert not [c for c in pip.calls if "-r" in c], "поставили без решённого набора"
        assert pip.json_urls == [], f"сеть тронули до решения набора: {pip.json_urls}"
        pip2 = Pip(rounds=[[("a", "1.0", "")]], ages={})
        EX._run = pip2.run
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert text == "" and "нет хеша" in why, (text, why)
    finally:
        importlib.reload(EX)


@test
def test_extras_no_quarantine_skips_the_wait_but_keeps_hashes(tmp: Path):
    """`--no-quarantine` (CI, срочное исправление): сети за возрастом нет, набор всё равно по хешам."""
    pip = Pip(rounds=[[("a", "1.0", "ha"), ("b", "2.0", "hb")]], ages={})
    EX, _venv = _patched(tmp, pip)
    try:
        text, why = EX.plan_lock(Path("python"), "a b", 0, NOW)
        assert why == "" and f"--hash=sha256:{HX('hb')}" in text and "release wait: 0 days" in text, (text, why)
        assert pip.json_urls == [], pip.json_urls
        import agent_core as AG
        seen = []
        saved = EX.install
        EX.install = lambda eid, days=None: seen.append((eid, days)) or {"ok": True, "log": "ok"}
        AG.sys.path.insert(0, str(SCRIPTS))
        try:
            sys.modules["aurora_extras"] = EX
            assert AG.cmd_venv_install(False) == 0 and seen == [("pydantic-ai", 0)], seen
            assert AG.cmd_venv_install(True) == 0 and seen[-1] == ("pydantic-ai", None), seen
        finally:
            EX.install = saved
    finally:
        importlib.reload(EX)


@test
def test_release_age_helpers_pick_by_hash_and_skip_prereleases(tmp: Path):
    """Возраст файла выпуска — по хешу; выпуск, которого нет на PyPI, возраста не имеет; предвыпуски и снятые не берутся."""
    pip = Pip(rounds=[], ages={("a", "1.0"): 10})
    EX, _venv = _patched(tmp, pip)
    try:
        assert abs(EX.release_age_days("a", "1.0", "h-a", NOW) - 10) < 0.01
        assert EX.release_age_days("private", "0.1", "", NOW) is None
        assert EX.aged_version("pip", 7, NOW) == "26.0", EX.aged_version("pip", 7, NOW)
        assert EX.aged_version("pip", 1, NOW) == "26.1"
        assert EX._epoch("2026-10-12T10:00:00.123456Z") == EX._epoch("2026-10-12T10:00:00Z")
    finally:
        importlib.reload(EX)


@test
def test_lock_files_are_pruned_and_private(tmp: Path):
    """Снимков хранится пять, новые первыми; файл только владельцу."""
    pip = Pip(rounds=[], ages={})
    EX, _venv = _patched(tmp, pip)
    try:
        for n in range(8):
            EX.save_lock("pydantic-ai", f"# снимок {n}\n")
        files = EX.lock_files("pydantic-ai")
        assert len(files) == EX.LOCK_KEEP == 5, files
        assert files[0].read_text(encoding="utf-8") == "# снимок 7\n", "новый снимок не первый"
        if os.name != "nt":
            assert (files[0].stat().st_mode & 0o777) == 0o600
    finally:
        importlib.reload(EX)


@test
def test_failed_compat_restores_the_whole_venv_from_the_previous_snapshot(tmp: Path):
    """Новая версия не прошла проверку: venv возвращается ЦЕЛИКОМ по снимку, а не одним верхним пакетом."""
    pip = Pip(rounds=[[("pydantic-ai", "9.0.0", "hp"), ("httpx", "1.0.0", "hh")]],
              ages={("pydantic-ai", "9.0.0"): 30, ("httpx", "1.0.0"): 30},
              freeze="pydantic-ai==2.51.0\nhttpx==0.28.1\npip==25.2\n-e git+x#egg=y\n")
    EX, venv = _patched(tmp, pip)
    try:
        res = EX.install("pydantic-ai", 7)
        assert not res["ok"] and res.get("rolled_back") == "9.0.0", res
        assert "venv возвращён целиком по снимку" in res["log"], res["log"]
        assert not any(c == f"pydantic-ai==2.51.0" for call in pip.calls for c in call), \
            "откат одним верхним пакетом вместо снимка"
        restored = [c for c in pip.calls if "-r" in c and "--no-deps" in c]
        assert len(restored) == 2, restored                      # установка нового и откат
        assert "httpx==0.28.1" in pip.locks[-1] and "-e git+" not in pip.locks[-1], pip.locks[-1]
        assert "--require-hashes" not in restored[-1], "у снимка до обновления хешей нет"
        assert not [p for p in EX.lock_files("pydantic-ai") if p.name.endswith("-install.txt")], \
            "снимок несовместимой установки сохранён"
    finally:
        importlib.reload(EX)


@test
def test_rollback_does_not_use_a_snapshot_of_another_version(tmp: Path):
    """venv правили мимо движка: снимок не про ту версию, что стоит, — откат одним верхним пакетом, а не «целиком» по чужому."""
    pip = Pip(rounds=[[("pydantic-ai", "9.0.0", "hp")]], ages={("pydantic-ai", "9.0.0"): 30},
              freeze="pydantic-ai==2.51.0\nhttpx==0.28.1\n")
    EX, _venv = _patched(tmp, pip)
    pip.have[0] = "2.40.0"
    try:
        res = EX.install("pydantic-ai", 7)
        assert not res["ok"] and res.get("rolled_back") == "9.0.0", res
        assert "venv возвращён целиком" not in res["log"], res["log"]
        assert any(c == "pydantic-ai==2.40.0" for call in pip.calls for c in call), "откат не вернул стоявшую версию"
    finally:
        importlib.reload(EX)


@test
def test_restore_keeps_the_old_venv_when_the_rebuild_fails(tmp: Path):
    """Откат по снимку не вышел — прежний venv возвращается на место, ничего не потеряно."""
    pip = Pip(rounds=[], ages={})
    EX, venv = _patched(tmp, pip)
    try:
        marker = venv / "marker.txt"
        marker.write_text("жив", encoding="utf-8")
        lock = tmp / "snap.txt"
        lock.write_text("httpx==0.28.1\n", encoding="utf-8")
        pip.fail_venv = True
        log = []
        assert EX.restore("pydantic-ai", lock, log) is False
        assert marker.read_text(encoding="utf-8") == "жив" and not venv.with_name("venv.prev").exists(), log
    finally:
        importlib.reload(EX)


@test
def test_age_lookup_failure_is_a_clean_refusal_and_a_locked_venv_survives(tmp: Path):
    """PyPI не ответил — отказ с причиной, а не traceback; venv, который нельзя переименовать (Windows), остаётся на месте."""
    pip = Pip(rounds=[[("a", "1.0", "ha")]], ages={})
    EX, venv = _patched(tmp, pip)
    try:
        def broken(url, timeout=15, limit=0):
            raise OSError("нет сети")
        EX._get_json = broken
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert text == "" and "не удалось узнать возраст выпусков" in why and "нет сети" in why, (text, why)
        # переименовать venv нельзя: он занят процессом
        marker = venv / "marker.txt"
        marker.write_text("жив", encoding="utf-8")
        lock = tmp / "snap.txt"
        lock.write_text("httpx==0.28.1\n", encoding="utf-8")
        real_replace = EX.os.replace

        def busy(src, dst):
            raise PermissionError("venv занят")
        EX.os.replace = busy
        log = []
        try:
            assert EX.restore("pydantic-ai", lock, log) is False
        finally:
            EX.os.replace = real_replace
        assert marker.read_text(encoding="utf-8") == "жив" and any("не удался" in x for x in log), log
    finally:
        importlib.reload(EX)


@test
def test_unknown_release_age_refuses_instead_of_letting_the_package_in(tmp: Path):
    """Возраст неизвестен — отказ: пустой ответ PyPI, 404 на файл с pythonhosted.org; частный индекс (404) пускается."""
    pip = Pip(rounds=[[("a", "1.0", "a" * 64)]] * 4, ages={("a", "1.0"): 90})
    EX, _venv = _patched(tmp, pip)
    try:
        pip.upload_json = {}
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert text == "" and "не удалось узнать возраст" in why and "время загрузки" in why, (text, why)
        pip.upload_json = None
        pip.url = "https://files.pythonhosted.org/packages/a-{v}.whl"
        pip.ages = {}                                       # PyPI о выпуске молчит (404), файл — с pythonhosted.org
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert text == "" and "PyPI не знает выпуска a 1.0" in why, (text, why)
        pip.url = "https://packages.example.org/a-{v}.whl"  # частный индекс: PyPI его не знает, и это не повод отказывать
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert why == "" and "a==1.0" in text, (text, why)
        # хеш файла не нашёлся среди файлов выпуска — возраст по самому СВЕЖЕМУ файлу (дозагруженное позже колесо)
        pip.ages = {("a", "1.0"): 90}
        pip.upload_json = {"urls": [{"digests": {"sha256": "x"}, "upload_time_iso_8601": _stamp(90)},
                                    {"digests": {"sha256": "y"}, "upload_time_iso_8601": _stamp(1)}]}
        assert EX.release_age_days("a", "1.0", "z" * 64, NOW) < 2
        assert abs(EX.release_age_days("a", "1.0", "x", NOW) - 90) < 0.01
    finally:
        importlib.reload(EX)


@test
def test_pip_report_is_untrusted_input_and_litellm_stays_out(tmp: Path):
    """Имя, версия и хеш из отчёта pip проверяются (перевод строки не дописывает директиву в снимок); LiteLLM и DSPy — отказ."""
    pip = Pip(rounds=[[("a", "1.0", "ab" * 32 + "\n--index-url http://evil.example.org")],
                      [("lite" + "llm", "1.0", "ab" * 32)], [("a\nb", "1.0", "ab" * 32)]], ages={})
    EX, _venv = _patched(tmp, pip)
    try:
        for expect in ("странное имя, версия или хеш", "который в надстройки не вводится", "странное имя, версия или хеш"):
            text, why = EX.plan_lock(Path("python"), "a", 0, NOW)
            assert text == "" and expect in why, (expect, text, why)
    finally:
        importlib.reload(EX)


@test
def test_a_package_without_a_wheel_is_resolved_once_more_without_the_ban(tmp: Path):
    """Решение идёт по готовым колёсам (`--only-binary`): сборка из исходников исполняет код до проверки возраста; нет колеса — повтор."""
    pip = Pip(rounds=[[("a", "1.0", "ab" * 32)]], ages={("a", "1.0"): 90})
    EX, _venv = _patched(tmp, pip)
    try:
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        first = next(c for c in pip.calls if "--report" in c)
        assert "--only-binary=:all:" in first and why == "" and "only-binary" in text, (first, text, why)
        lock = tmp / "l.txt"
        lock.write_text(text, encoding="utf-8")
        assert "--only-binary=:all:" in EX._install_lock(Path("python"), lock).args
        pip.fail_binary, pip.rounds[:] = True, [[("a", "1.0", "ab" * 32)]]
        pip.url = "https://files.example.org/a-{v}.tar.gz"
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert why == "" and "only-binary" not in text, (text, why)       # повтор без запрета, снимок без пометки
        lock.write_text(text, encoding="utf-8")
        assert "--only-binary=:all:" not in EX._install_lock(Path("python"), lock).args
        # повтор виден в журнале: сказано, что разрешена сборка и какой пакет из исходников
        pip.fail_binary, pip.rounds[:] = True, [[("a", "1.0", "ab" * 32)]]
        said = []
        EX.plan_lock(Path("python"), "a", 7, NOW, log=said)
        assert any("сборку из исходников" in x for x in said) and any("соберутся: a" in x for x in said), said
    finally:
        importlib.reload(EX)


@test
def test_a_network_failure_is_not_a_reason_to_allow_source_builds(tmp: Path):
    """Повтор без `--only-binary` только когда pip сказал «нет колеса», а не при обрыве связи или сбое прокси."""
    pip = Pip(rounds=[[("a", "1.0", "ab" * 32)]], ages={("a", "1.0"): 90})
    EX, _venv = _patched(tmp, pip)
    real = pip.run

    def net_down(cmd, timeout=60):
        if "--report" in cmd:
            pip.calls.append([str(c) for c in cmd])
            return subprocess.CompletedProcess(cmd, 1, "", "WARNING: Retrying (Retry(total=4)) after connection broken\n"
                                               "ERROR: Could not find a version that satisfies the requirement a")
        return real(cmd, timeout)
    try:
        EX._run = net_down
        text, why = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert text == "" and why, (text, why)
        assert sum(1 for c in pip.calls if "--report" in c) == 1, "после обрыва связи набор решается второй раз"
        # имя пакета со словом «network»/«ssl» — не сбой сети: «нет колеса» остаётся причиной повтора
        def no_wheel_named(cmd, timeout=60):
            if "--report" in cmd and "--only-binary=:all:" in cmd:
                pip.calls.append([str(c) for c in cmd])
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: No matching distribution found for networkx==3.0 (pyopenssl proxy)")
            return real(cmd, timeout)
        EX._run = no_wheel_named
        pip.rounds[:] = [[("networkx", "3.0", "ab" * 32)]]
        pip.calls.clear()
        said = []
        text, why = EX.plan_lock(Path("python"), "networkx", 7, NOW, log=said)
        assert sum(1 for c in pip.calls if "--report" in c) == 2 and said, (pip.calls, said)
    finally:
        importlib.reload(EX)


@test
def test_snapshots_are_checked_when_read_and_every_pip_runs_isolated(tmp: Path):
    """Снимок читается с диска: чужая директива pip (`--index-url`, `-e`) в нём не принимается; папка снимков только
    владельцу; весь `-m pip` и `-m venv` надстроек идёт с `-I`, чтобы папка запуска не стала первой в sys.path."""
    pip = Pip(rounds=[[("a", "1.0", "ab" * 32)]], ages={("a", "1.0"): 90}, freeze="a==1.0\n")
    EX, _venv = _patched(tmp, pip)
    try:
        good, _ = EX.plan_lock(Path("python"), "a", 7, NOW)
        assert EX.lock_text_problem(good) == "", good
        assert EX.lock_text_problem(EX.freeze_text(Path("python"))) == ""
        assert good.isascii() and EX.freeze_text(Path("python")).isascii(), "pip читает файл в кодировке локали"
        for evil in ("--index-url https://example.org/simple\n", "-e .\n", "a==1.0 --hash=sha256:abc\n",
                     "a==1.0\n--extra-index-url https://example.org\n", "vcs+https://example.org/x.tar\n"):
            assert EX.lock_text_problem(good + evil), evil
            f = tmp / "evil.txt"
            f.write_text(good + evil, encoding="utf-8")
            before = len(pip.calls)
            res = EX._install_lock(Path("python"), f)
            assert res.returncode == 2 and len(pip.calls) == before, "pip запущен по испорченному снимку"
        EX.save_lock("pydantic-ai", good)
        if os.name != "nt":
            assert ((EX.LOCKS / "pydantic-ai").stat().st_mode & 0o777) == 0o700
        pip.rounds[:] = [[("pydantic-ai", "2.51.0", "ab" * 32)]]
        pip.ages[("pydantic-ai", "2.51.0")] = 90
        EX.install("pydantic-ai", 7)
        seen = [c for c in pip.calls if "-m" in c and ("pip" in c or "venv" in c)]
        assert seen, pip.calls
        for c in seen:
            assert "-I" in c[:c.index("-m")], c
            if "pip" in c[c.index("-m"):]:
                assert c[c.index("-m") - 2:c.index("-m")] == ["-X", "utf8"], c       # -I отбросил бы PYTHONUTF8
    finally:
        importlib.reload(EX)


@test
def test_restore_never_destroys_the_live_venv_and_replaces_it_when_it_can(tmp: Path):
    """Остаток прежнего `.prev`, исключение подпроцесса, успешная замена: живой venv не теряется, при успехе заменён."""
    pip = Pip(rounds=[], ages={})
    EX, venv = _patched(tmp, pip)
    try:
        (venv / "marker.txt").write_text("жив", encoding="utf-8")
        lock = tmp / "snap.txt"
        lock.write_text("httpx==0.28.1\n", encoding="utf-8")
        stale = venv.with_name("venv.prev")                  # остаток прежней попытки (имя старой схемы)
        stale.mkdir()
        (stale / "stale.txt").write_text("мусор", encoding="utf-8")
        real_replace = EX.os.replace

        def refuse(src, dst):
            raise PermissionError("занят")
        EX.os.replace = refuse
        try:
            assert EX.restore("pydantic-ai", lock, []) is False
        finally:
            EX.os.replace = real_replace
        assert (venv / "marker.txt").read_text(encoding="utf-8") == "жив" and not (venv / "stale.txt").exists()
        # таймаут подпроцесса: исключение не вылетает, прежний venv возвращён на место
        real_run = pip.run

        def slow(cmd, timeout=60):
            if "-m" in cmd and "venv" in [str(c) for c in cmd]:
                raise subprocess.TimeoutExpired(cmd, timeout)
            return real_run(cmd, timeout)
        EX._run = slow
        log = []
        assert EX.restore("pydantic-ai", lock, log) is False
        assert (venv / "marker.txt").read_text(encoding="utf-8") == "жив" and not list(tmp.glob("venv.prev-*")), log
        # успех: venv заменён новым, отложенный прежний убран
        EX._run = pip.run
        assert EX.restore("pydantic-ai", lock, []) is True
        assert venv.is_dir() and not (venv / "marker.txt").exists() and not list(tmp.glob("venv.prev-*"))
        assert pip.locks and "httpx==0.28.1" in pip.locks[-1]
    finally:
        importlib.reload(EX)


@test
def test_two_installs_of_one_extra_do_not_run_at_once_and_snapshots_never_collide(tmp: Path):
    """Вторая установка той же надстройки отказывает, пока идёт первая; снимки двух процессов не затирают друг друга."""
    pip = Pip(rounds=[], ages={})
    EX, _venv = _patched(tmp, pip)
    try:
        from aurora_common import lock_release, lock_take
        guard = EX.LOCKS / "pydantic-ai.install.lock"
        assert lock_take(guard, {"what": "other"})[0]
        busy = EX.install("pydantic-ai", 7)
        assert not busy["ok"] and "уже идёт" in busy["log"], busy
        lock_release(guard)
        a, b = EX.save_lock("pydantic-ai", "# a\n"), EX.save_lock("pydantic-ai", "# b\n")
        assert a != b and a.read_text(encoding="utf-8") == "# a\n" and b.read_text(encoding="utf-8") == "# b\n"
    finally:
        importlib.reload(EX)


@test
def test_status_offers_the_release_the_install_will_actually_take(tmp: Path):
    """Кнопка «Обновить» называет выпуск старше паузы, а не самый свежий на PyPI (который установка не возьмёт)."""
    pip = Pip(rounds=[], ages={})
    EX, _venv = _patched(tmp, pip)
    try:
        EX.CACHE = tmp / "cache.json"
        EX.installed_version = lambda _id: "2.0.0"
        data = {"info": {"version": "2.2.0"},
                "releases": {"2.0.0": [{"upload_time_iso_8601": _stamp(90)}],
                             "2.1.0": [{"upload_time_iso_8601": _stamp(30)}],
                             "2.2.0": [{"upload_time_iso_8601": _stamp(1)}]}}
        EX._get_json = lambda url, timeout=15, limit=0: data if "pypi.org/pypi/" in url else {"tag_name": "v2.2.0"}
        row = next(r for r in EX.status(fresh=True) if r["id"] == "pydantic-ai")
        assert row["pypi"] == "2.2.0" and row["target"] == "2.1.0" and row["update"] is True, row
        EX.installed_version = lambda _id: "2.1.0"
        row = next(r for r in EX.status() if r["id"] == "pydantic-ai")
        assert row["update"] is False, "стоит всё, что установка готова взять: обновлять нечего"
    finally:
        importlib.reload(EX)


@test
def test_engine_dependencies_are_pinned_and_agree_with_ci(tmp: Path):
    """requirements.txt закрепляет bs4 и markdownify теми же версиями, что CI; незакреплённой строки нет."""
    EX = importlib.import_module("aurora_extras")
    req = KIT / "requirements.txt"
    lines = [ln.strip() for ln in req.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9][\w.\-]*==\d+(\.\d+)+", ln) for ln in lines), lines
    pins = EX.requirements_pins(req)
    ci = EX.requirements_pins(KIT / ".github" / "requirements-ci.txt")
    assert set(pins) == {"beautifulsoup4", "markdownify"}, pins
    for name, ver in pins.items():
        assert ci.get(name) == ver, f"{name}: requirements.txt {ver}, CI {ci.get(name)} — обновляйте оба файла"
    assert EX.requirements_pins(tmp / "нет-такого.txt") == {}
    assert EX.pinned_spec("beautifulsoup4") == f"beautifulsoup4=={pins['beautifulsoup4']}"
    assert EX.pinned_spec("Markdownify") == f"Markdownify=={pins['markdownify']}"
    assert EX.pinned_spec("lxml") == ""


@test
def test_panel_installs_pinned_or_aged_python_packages(tmp: Path):
    """Панель ставит закреплённое китом, а остальное — самый новый выпуск старше паузы (на старом коде — любое последнее)."""
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    EX = importlib.import_module("aurora_extras")
    importlib.reload(EX)
    seen = []
    real_run = ck.subprocess.run
    real_env = ck.environment
    real_aged = EX.aged_version

    def spy(cmd, *a, **kw):
        seen.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    ck.subprocess.run = spy
    ck.environment = lambda fresh=False: {"items": [{"pip": "lxml", "ok": True}, {"pip": "beautifulsoup4", "ok": True}]}
    EX.aged_version = lambda dist, days, now=0.0: "5.0.0"
    try:
        out = ck.env_install("beautifulsoup4")
        assert out["ok"] and seen[-1][-1] == f"beautifulsoup4=={EX.requirements_pins()['beautifulsoup4']}", seen
        out = ck.env_install("lxml")
        assert out["ok"] and seen[-1][-1] == "lxml<=5.0.0", seen
        # -P только там, где он есть (Python 3.11+); markdownify ставится вместе с закреплённым beautifulsoup4
        assert ("-P" in seen[-1]) == (sys.version_info >= (3, 11)), seen[-1]
        pins = EX.requirements_pins()
        ck.environment = lambda fresh=False: {"items": [{"pip": "markdownify", "ok": True}]}
        out = ck.env_install("markdownify")
        assert seen[-1][-2:] == [f"markdownify=={pins['markdownify']}", f"beautifulsoup4=={pins['beautifulsoup4']}"], seen[-1]
        EX.aged_version = lambda dist, days, now=0.0: (_ for _ in ()).throw(OSError("нет сети"))
        out = ck.env_install("lxml")
        assert seen[-1][-1] == "lxml" and "паузу после выпуска проверить не удалось" in out["output"], (seen, out)
        assert "beautifulsoup4==" in ck.pip_command("beautifulsoup4") and ck.pip_command("lxml").endswith("install lxml")
    finally:
        ck.subprocess.run, ck.environment, EX.aged_version = real_run, real_env, real_aged
        importlib.reload(ck)
        importlib.reload(EX)


@test
def test_doctor_warns_when_a_pinned_dependency_drifted(tmp: Path):
    """kit:doctor называет закреплённую зависимость, стоящую в другой версии; совпадающую и отсутствующую — нет."""
    import aurora_doctor as D
    from importlib import metadata
    EX = importlib.import_module("aurora_extras")
    real_pins, real_version = EX.requirements_pins, metadata.version
    EX.requirements_pins = lambda path=None: {"beautifulsoup4": "1.0.0", "markdownify": "2.0.0", "ghost-pkg": "3.0.0"}

    def version(name):
        if name == "ghost-pkg":
            raise metadata.PackageNotFoundError(name)
        return {"beautifulsoup4": "1.0.0", "markdownify": "2.1.0"}[name]
    metadata.version = version
    try:
        warns = D.check_pins()
    finally:
        EX.requirements_pins, metadata.version = real_pins, real_version
    assert len(warns) == 1 and warns[0].startswith("markdownify 2.1.0, а кит закрепил 2.0.0"), warns


@test
def test_extras_do_not_pull_litellm_or_dspy(_t):
    """Решение по цепочке поставок: LiteLLM и DSPy (он тянет LiteLLM) в venv надстроек не вводятся."""
    EX = importlib.import_module("aurora_extras")
    for eid, ex in EX.EXTRAS.items():
        specs = " ".join(ex["pip"]).casefold()
        assert "litellm" not in specs and "dspy" not in specs, (eid, specs)
        assert ex["min_python"] >= (3, 10), "Pydantic AI с 1.0 требует 3.10+: на старом pip берёт уязвимую ветку 0.x"


@test
def test_ci_installs_fresh_extras_without_quarantine_and_audits_them(tmp: Path):
    """canary ставит надстройку без паузы (он и нужен, чтобы видеть свежее), следит за уязвимостями и не краснит из-за них."""
    text = (KIT / ".github" / "workflows" / "canary.yml").read_text(encoding="utf-8")
    assert "--venv-install --no-quarantine" in text
    step = text[text.index("Known vulnerabilities in the extras venv"):]
    step = step[:step.index("- name: Adapter self-check")]
    assert "continue-on-error: true" in step and "pip-audit" in step, step
    assert "requirements-latest.txt" in text and "pip install --quiet -r requirements.txt" not in text, \
        "canary ставит закреплённый requirements.txt и свежих версий bs4 не увидит"
