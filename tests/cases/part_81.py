"""Проверки движка Aurora, часть 81: первые потребители ядра надёжности записи (BL-SEC-25).

Каркас и помощники — tests/harness.py. Записи «что идёт» (`.running.json`) читаются-меняются-пишутся из потоков
панели, Cron и второй панели: под замком ОС они не теряют друг друга; повреждённое состояние Cron и записей
переносится в `.corrupt`, а не стирается следующей записью; файл токена панели и настройка моделей (ключи)
рождаются с правами 600; осиротевшие временные файлы убираются при старте панели. Настоящий кит, `~` и сеть
проверкам не нужны: пути подменены на временные.
"""
from __future__ import annotations

from pathlib import Path
import contextlib
import io
import json
import os
import stat
import sys
import threading
import time

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401


def _mods():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import aurora_cockpit as ck
    import aurora_fs as F
    return ck, F


@test
def test_running_registry_keeps_every_mark_under_concurrent_writers(tmp: Path):
    """Шесть потоков по 20 раз отмечают «идёт»: ни одна отметка не потеряна (чтение-правка-запись без замка теряла их),
    файл всегда разбирается целиком, снятие отметок опустошает список."""
    ck, F = _mods()
    saved = ck.RUNNING
    ck.RUNNING = str(tmp / "running.json")
    bad, errors = [], []
    stop = threading.Event()

    def mark(k):
        try:
            for n in range(20):
                ck.mark_running(f"job-{k}-{n}", f"cmd{k}", str(tmp / "proj"), True)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def watch():
        while not stop.is_set():
            time.sleep(0.002)                        # не держать диск плотным опросом: писатели ждут замок
            try:
                json.loads(Path(ck.RUNNING).read_text(encoding="utf-8"))
            except OSError:
                pass                                 # Windows: файл в миг подмены занят — читатель пробует снова
            except ValueError as e:
                bad.append(str(e))

    try:
        watcher = threading.Thread(target=watch)
        threads = [threading.Thread(target=mark, args=(k,)) for k in range(6)]
        watcher.start()
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        stop.set()
        watcher.join(30)
        assert not errors, errors[:3]
        assert not bad, f"читатель увидел недописанный файл: {bad[:2]}"
        got = ck.running_now()
        assert len(got) == 120, f"отметок {len(got)} вместо 120 — потерянные обновления"
        assert all(v["project"] == "proj" and v["panel"] == os.getpid() for v in got.values()), list(got.values())[:2]
        for k in range(6):
            for n in range(20):
                ck.mark_running(f"job-{k}-{n}", "", "", False)
        assert ck.running_now() == {}
        assert not [p for p in tmp.iterdir() if p.name.endswith(".aurora-tmp")], list(tmp.iterdir())
    finally:
        ck.RUNNING = saved


@test
def test_a_damaged_running_registry_is_kept_aside_and_the_panel_starts_clean(tmp: Path):
    """Битый `.running.json` не стирается молча: он уходит в `.corrupt`, список пуст, новая отметка пишется заново."""
    ck, F = _mods()
    saved = ck.RUNNING
    ck.RUNNING = str(tmp / "running.json")
    Path(ck.RUNNING).write_text('{"job": {"cmd": ', encoding="utf-8")
    past = time.time() - 30                          # карантин не трогает молодой файл: его может дописывать прежний код
    os.utime(ck.RUNNING, (past, past))
    F._WARNED.clear()
    try:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            assert ck.running_now() == {}
        assert "running.json" in err.getvalue() and ".corrupt" in err.getvalue(), err.getvalue()
        assert (tmp / "running.json.corrupt").read_text(encoding="utf-8") == '{"job": {"cmd": '
        ck.mark_running("j1", "kb:fix", str(tmp), True)
        assert list(ck.running_now()) == ["j1"]
        ck.drop_dead_running()                       # запись своей же панели — не «живая чужая»: список очищается
        assert ck.running_now() == {}
    finally:
        ck.RUNNING = saved
        F._WARNED.clear()


@test
def test_cron_state_is_read_through_the_core_and_a_damaged_file_is_not_forgotten(tmp: Path):
    """Расписание Cron: запись целиком, чтение с карантином. Битый файл заданий переносится в `.corrupt` — следующее
    сохранение не затирает его без следа."""
    ck, F = _mods()
    import cron as CRON
    path = str(tmp / "cron-tasks.json")
    CRON._write_json(path, {"tasks": [{"id": "а", "name": "ночной разбор"}]})
    assert CRON._read_json(path, {}) == {"tasks": [{"id": "а", "name": "ночной разбор"}]}
    assert "ночной разбор" in Path(path).read_text(encoding="utf-8"), "русский текст записан escape-кодами"
    Path(path).write_text('{"tasks": [{"id"', encoding="utf-8")
    past = time.time() - 30
    os.utime(path, (past, past))
    F._WARNED.clear()
    with contextlib.redirect_stderr(io.StringIO()):
        assert CRON._read_json(path, {}) == {}
    CRON._write_json(path, {"tasks": []})
    assert (tmp / "cron-tasks.json.corrupt").read_text(encoding="utf-8") == '{"tasks": [{"id"', "битое расписание затёрто"
    assert CRON._read_json(path, {}) == {"tasks": []}
    assert CRON._read_json(path, []) == [], "файл-словарь принят за список"


@test
def test_secret_files_are_born_with_private_rights(tmp: Path):
    """Токен панели (`.session.json`) и настройка моделей (ключи) создаются сразу с правами 600: окна, когда файл
    записан, но открыт всем, нет; временных остатков нет."""
    ck, F = _mods()
    import model_config as MC
    seen = []
    real = os.fsync

    def peek(fd):
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):                 # fsync каталога (запись имени) — не файл секрета
            seen.append(st.st_mode & 0o777)
        return real(fd)
    saved = ck.SESSION
    ck.SESSION = str(tmp / "session.json")
    os.fsync = peek
    try:
        ck.write_session(8765, "http://127.0.0.1:8765/")
        kit = tmp / "kit"
        (kit / "local").mkdir(parents=True)
        res = MC.save(kit, {"providers": [{"id": "gw1", "name": "Шлюз", "url": "http://gw.example.com/v1",
                                           "key": "секрет", "enabled": True}]})
    finally:
        os.fsync = real
        ck.SESSION = saved
    assert res["ok"], res
    assert json.loads((tmp / "session.json").read_text(encoding="utf-8"))["port"] == 8765
    assert json.loads((kit / "local" / "models.json").read_text(encoding="utf-8"))["providers"][0]["key"] == "секрет"
    if os.name != "nt":
        assert seen and set(seen) == {0o600}, f"секрет рождён с правами {[oct(m) for m in seen]}"
        assert (tmp / "session.json").stat().st_mode & 0o777 == 0o600
        assert (kit / "local" / "models.json").stat().st_mode & 0o777 == 0o600
    for folder in (tmp, kit / "local"):
        assert not [p for p in folder.iterdir() if p.name.endswith((".tmp", ".aurora-tmp"))], list(folder.iterdir())


@test
def test_the_panel_sweeps_orphaned_temp_files_of_its_state_folders(tmp: Path):
    """При старте панель убирает осиротевшие временные файлы записи в `cockpit/` и `~/.aurora` (старше часа), свежие и
    чужие имена не трогает."""
    ck, F = _mods()
    home = tmp / "home"
    (home / ".aurora").mkdir(parents=True)
    kit_state = tmp / "cockpit"
    kit_state.mkdir()
    restore = set_home(home)
    saved = ck.RUNNING
    ck.RUNNING = str(kit_state / ".running.json")
    try:
        old = [kit_state / ".1-2-3.aurora-tmp", home / ".aurora" / "cron-state.json.tmp"]
        keep = [kit_state / ".4-5-6.aurora-tmp", home / ".aurora" / "cron-tasks.json", kit_state / "notes.tmp.txt"]
        for p in old + keep:
            p.write_text("x", encoding="utf-8")
        past = time.time() - 7200
        for p in old + [keep[1], keep[2]]:
            os.utime(p, (past, past))
        gone = ck.sweep_state_tmp()
        assert sorted(gone) == sorted(p.name for p in old), gone
        assert all(not p.exists() for p in old) and all(p.exists() for p in keep), [p.name for p in keep if not p.exists()]
    finally:
        ck.RUNNING = saved
        restore()
