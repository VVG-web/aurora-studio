"""Проверки движка Aurora, часть 80: ядро надёжности записи `aurora_fs` (BL-SEC-25).

Каркас и помощники — tests/harness.py. Один модуль на стандартной библиотеке даёт атомарную запись файла,
замок ОС и чтение состояния с карантином повреждённого файла. Здесь проверяется сам модуль: обрыв записи,
гонки потоков и процессов, смерть держателя замка, перенос повреждённого состояния, уборка осиротевших
временных файлов, подключение через `aurora_common` и место в манифесте движка. Домашняя папка и сеть
проверкам не нужны.
"""
from __future__ import annotations

from pathlib import Path
import ast
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import threading
import time

from harness import KIT, SCRIPTS, test  # noqa: F401


def _fs():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("aurora_fs")


def _leftovers(folder: Path) -> list:
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".aurora-tmp") or p.name.endswith(".tmp"))


@test
def test_write_atomic_replaces_whole_file_and_leaves_no_temp(tmp: Path):
    """Файл пишется целиком: текст и байты, русские буквы, вложенные каталоги; временных файлов не остаётся."""
    F = _fs()
    p = tmp / "a" / "b" / "state.txt"
    F.write_atomic(p, "привет\nмир\n")
    assert p.read_bytes() == "привет\nмир\n".encode("utf-8")
    F.write_atomic(p, b"\x00\x01raw")
    assert p.read_bytes() == b"\x00\x01raw"
    F.write_json(tmp / "s.json", {"имя": "значение", "n": [1, 2]})
    assert json.loads((tmp / "s.json").read_text(encoding="utf-8")) == {"имя": "значение", "n": [1, 2]}
    assert "значение" in (tmp / "s.json").read_text(encoding="utf-8"), "русские буквы записаны escape-кодами"
    assert not _leftovers(p.parent) and not _leftovers(tmp)
    F.write_atomic(p, "lone \udc80 surrogate\n")
    assert p.read_bytes(), "непарный суррогат уронил запись"


@test
def test_write_atomic_newline_modes(tmp: Path):
    """`newline`: '' — как есть; '\\r\\n' — CRLF; 'keep' — как у файла на диске; None — перевод строки платформы."""
    F = _fs()
    p = tmp / "n.txt"
    F.write_atomic(p, "a\r\nb\n")
    assert p.read_bytes() == b"a\r\nb\n", "режим по умолчанию изменил концы строк"
    F.write_atomic(p, "a\nb\n", newline="\r\n")
    assert p.read_bytes() == b"a\r\nb\r\n"
    F.write_atomic(p, "x\ny\n", newline="keep")
    assert p.read_bytes() == b"x\r\ny\r\n", "файл был CRLF — запись обязана остаться CRLF"
    F.write_atomic(p, "x\r\ny\n", newline="")
    F.write_atomic(p, "q\r\nw\n", newline="keep")
    assert p.read_bytes() == b"q\nw\n", "файл LF после правки не должен стать CRLF"
    F.write_atomic(p, "m\r\nn\n", newline=None)
    assert p.read_bytes() == ("m\nn\n".replace("\n", os.linesep)).encode("utf-8"), p.read_bytes()
    try:
        F.write_atomic(p, "z", newline="zzz")
    except ValueError:
        pass
    else:
        raise AssertionError("неизвестный newline принят")


@test
def test_write_atomic_modes_never_widen(tmp: Path):
    """Права: прежние файла сохраняются; явный `mode` (секрет) действует с рождения файла — окна с широкими правами нет."""
    if os.name == "nt":
        return                                   # биты POSIX там не действуют
    F = _fs()
    p = tmp / "m.txt"
    p.write_text("старое", encoding="utf-8")
    p.chmod(0o640)
    F.write_atomic(p, "новое")
    assert p.stat().st_mode & 0o777 == 0o640, oct(p.stat().st_mode)
    seen = []
    real = os.fsync

    def peek(fd):
        seen.append(os.fstat(fd).st_mode & 0o777)    # временный файл уже записан, ещё не подменён
        return real(fd)
    os.fsync = peek
    try:
        F.write_atomic(tmp / "secret.env", "TOKEN=x\n", mode=0o600)
    finally:
        os.fsync = real
    assert seen and seen[0] == 0o600, f"секрет рождён с правами {oct(seen[0]) if seen else '?'}"
    assert (tmp / "secret.env").stat().st_mode & 0o777 == 0o600


@test
def test_write_atomic_interrupted_keeps_old_file_and_cleans_temp(tmp: Path):
    """Сбой на `fsync` и прерывание на подмене: прежний файл цел, временного файла нет, ошибка не проглочена."""
    F = _fs()
    p = tmp / "state.json"
    F.write_atomic(p, "старое\n")
    real_sync, real_replace = os.fsync, F.replace_file
    try:
        def disk_full(fd):
            raise OSError(28, "No space left on device")
        os.fsync = disk_full
        try:
            F.write_atomic(p, "новое\n")
        except OSError as e:
            assert e.errno == 28, e
        else:
            raise AssertionError("сбой диска проглочен")
        os.fsync = real_sync

        def stop(*a, **kw):
            raise KeyboardInterrupt
        F.replace_file = stop
        try:
            F.write_atomic(p, "новое\n")
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("прерывание проглочено")
    finally:
        os.fsync, F.replace_file = real_sync, real_replace
    assert p.read_text(encoding="utf-8") == "старое\n", "прежний файл изменён"
    assert not _leftovers(tmp), _leftovers(tmp)


@test
def test_write_atomic_threads_never_mix_texts_or_share_a_temp_name(tmp: Path):
    """Четыре потока пишут один файл: читатель видит один из текстов целиком, остатков нет, ошибок нет."""
    F = _fs()
    p = tmp / "shared.txt"
    texts = [c * (9000 + 3000 * i) + "\n" for i, c in enumerate("ABCD")]
    F.write_atomic(p, texts[0])
    stop, bad, errors = threading.Event(), [], []

    def writer(text):
        try:
            for _ in range(60):
                F.write_atomic(p, text, fsync=False)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def reader():
        while not stop.is_set():
            data = p.read_bytes().decode("utf-8")
            if data not in texts:
                bad.append(len(data))

    watch = threading.Thread(target=reader)
    ws = [threading.Thread(target=writer, args=(t,)) for t in texts]
    watch.start()
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    stop.set()
    watch.join()
    assert not errors, errors[:3]
    assert not bad, f"читатель увидел смесь текстов (длины {bad[:3]})"
    assert not _leftovers(tmp), _leftovers(tmp)


@test
def test_write_atomic_follows_a_link_unless_told_otherwise(tmp: Path):
    """Ссылка по умолчанию не заменяется — пишется файл, на который она ведёт; `follow_links=False` заменяет саму ссылку."""
    F = _fs()
    real = tmp / "real.txt"
    real.write_text("цель", encoding="utf-8")
    link = tmp / "link.txt"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        return                                   # символические ссылки недоступны (Windows без прав)
    F.write_atomic(link, "через ссылку")
    assert link.is_symlink() and real.read_text(encoding="utf-8") == "через ссылку"
    F.write_atomic(link, "саму ссылку", follow_links=False)
    assert not link.is_symlink() and link.read_text(encoding="utf-8") == "саму ссылку"
    assert real.read_text(encoding="utf-8") == "через ссылку", "цель ссылки затронута"


_BUMP = """
import sys
sys.path.insert(0, sys.argv[1])
import aurora_fs as F
path = sys.argv[2]
for _ in range(int(sys.argv[3])):
    with F.FileLock(path + ".lock", timeout=60):
        data = F.read_state(path, {"n": 0})
        data["n"] += 1
        F.write_json(path, data, fsync=False)
"""

_HOLD = """
import sys, time
sys.path.insert(0, sys.argv[1])
import aurora_fs as F
lock = F.FileLock(sys.argv[2], info={"what": "держу для проверки"})
assert lock.acquire(timeout=10)
print("held", flush=True)
time.sleep(120)
"""


@test
def test_file_lock_serializes_processes_without_lost_updates(tmp: Path):
    """Четыре процесса по 25 раз читают-меняют-пишут один счётчик под замком: потерянных обновлений нет."""
    _fs()
    counter = tmp / "counter.json"
    procs = [subprocess.Popen([sys.executable, "-c", _BUMP, str(SCRIPTS), str(counter), "25"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    errs = []
    for pr in procs:
        _out, err = pr.communicate(timeout=180)
        if pr.returncode:
            errs.append(err[-400:])
    assert not errs, errs
    assert json.loads(counter.read_text(encoding="utf-8")) == {"n": 100}, counter.read_text(encoding="utf-8")
    assert not _leftovers(tmp), _leftovers(tmp)


@test
def test_file_lock_threads_reentry_timeout_and_owner(tmp: Path):
    """Тот же поток входит повторно; другой поток ждёт и по таймауту получает `LockTimeout` с именем владельца."""
    F = _fs()
    path = tmp / "t.lock"
    with F.FileLock(path, info={"what": "правка карточки"}) as outer:
        assert outer.held and F.is_locked(path)
        with F.FileLock(path):                          # вложенный вход того же потока
            assert F.is_locked(path)
        assert F.is_locked(path), "вложенный выход снял внешний замок"
        assert "правка карточки" in F.describe_owner(path) and str(os.getpid()) in F.describe_owner(path)
        got = {}

        def other():
            lock = F.FileLock(path, timeout=0.2)
            got["nonblocking"] = lock.acquire(blocking=False)
            try:
                with lock:
                    got["entered"] = True
            except F.LockTimeout as e:
                got["timeout"] = str(e)

        t = threading.Thread(target=other)
        t.start()
        t.join(30)
        assert got.get("nonblocking") is False and not got.get("entered"), got
        assert "правка карточки" in got.get("timeout", ""), got
    assert not F.is_locked(path) and path.read_text(encoding="utf-8") == "", "после выхода замок числится занятым"
    after = []

    def take_and_free():
        lock = F.FileLock(path)
        after.append(lock.acquire(blocking=False))
        lock.release()
    t2 = threading.Thread(target=take_and_free)
    t2.start()
    t2.join(30)
    assert after == [True], "замок не отпущен после выхода"


@test
def test_file_lock_dies_with_its_process(tmp: Path):
    """Держатель убит (`kill`): замок не «брошен» — следующий берёт его сразу, без проверок pid и таймаутов давности."""
    F = _fs()
    path = tmp / "d.lock"
    holder = subprocess.Popen([sys.executable, "-c", _HOLD, str(SCRIPTS), str(path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held", holder.stderr.read()
        assert F.is_locked(path), "замок живого держателя не виден"
        assert "держу для проверки" in F.describe_owner(path)
        t0 = time.monotonic()
        assert F.FileLock(path).acquire(timeout=0.3) is False
        assert time.monotonic() - t0 < 5
    finally:
        holder.kill()
        holder.wait(30)
    again = F.FileLock(path)
    assert again.acquire(timeout=20), "замок убитого процесса не освободился"
    again.release()


@test
def test_file_lock_without_os_locks_degrades_only_when_optional(tmp: Path):
    """Файловая система без замков: по запросу работа идёт без замка (одно предупреждение), иначе — понятный отказ."""
    F = _fs()
    real = F._try_lock

    def unsupported(fd):
        raise F.LockUnsupported(95, "нет замков")
    F._try_lock = unsupported
    F._WARNED.clear()
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with F.FileLock(tmp / "o.lock", optional=True) as lock:
                assert lock.degraded and lock.held
            with F.FileLock(tmp / "o.lock", optional=True):
                pass
        assert err.getvalue().count("нет замков ОС") == 1, err.getvalue()
        try:
            F.FileLock(tmp / "o2.lock").acquire()
        except F.LockUnsupported:
            pass
        else:
            raise AssertionError("без замков ОС обязательный замок выдан")
    finally:
        F._try_lock = real
        F._WARNED.clear()
    fine = F.FileLock(tmp / "o3.lock")
    assert fine.acquire(timeout=1), "после подмены замок не работает"
    fine.release()


_FORK = """
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import aurora_fs as F
lock = F.FileLock(sys.argv[2])
assert lock.acquire(timeout=10)
pid = os.fork()
if pid == 0:
    got = F.FileLock(sys.argv[2]).acquire(blocking=False)
    os._exit(1 if got else 0)               # ребёнок не должен получить замок родителя
_, status = os.waitpid(pid, 0)
probe = ("import sys; sys.path.insert(0, sys.argv[1]); import aurora_fs as F; "
         "sys.exit(0 if not F.FileLock(sys.argv[2]).acquire(blocking=False) else 1)")
still = subprocess.run([sys.executable, "-c", probe, sys.argv[1], sys.argv[2]]).returncode == 0
print("child_blocked" if os.WEXITSTATUS(status) == 0 else "child_got_lock", "held" if still else "released", flush=True)
"""


@test
def test_file_lock_one_object_relative_path_alias_and_info_edge_cases(tmp: Path):
    """Один объект замка в двух потоках не пускает второй; вложенный выход не снимает внешний; относительный путь
    переживает смену рабочей папки; жёсткая ссылка — тот же замок; странное описание владельца замок не ломает;
    флаг «без замка» не залипает."""
    F = _fs()
    path = tmp / "one.lock"
    shared = F.FileLock(path, timeout=0.3)
    with shared:
        with shared:
            pass
        assert F.is_locked(path), "внутренний выход снял внешний замок"
        res = {}

        def other():
            res["got"] = shared.acquire(blocking=False)
        t = threading.Thread(target=other)
        t.start()
        t.join(30)
        assert res["got"] is False, "тот же объект в другом потоке вошёл без ожидания"
    assert not F.is_locked(path)

    (tmp / "a").mkdir()
    (tmp / "b").mkdir()
    here = os.getcwd()
    os.chdir(tmp / "a")
    try:
        rel = F.FileLock("rel.lock")
        assert rel.acquire()
        os.chdir(tmp / "b")                                      # движок меняет рабочую папку в потоках
        rel.release()
    finally:
        os.chdir(here)
    assert not F.is_locked(tmp / "a" / "rel.lock"), "относительный путь: замок остался после смены папки"

    first = tmp / "first.lock"
    first.write_text("", encoding="utf-8")
    second = tmp / "second.lock"
    try:
        os.link(first, second)
    except (OSError, NotImplementedError, AttributeError):
        second = None
    if second is not None:
        with F.FileLock(first):
            with F.FileLock(second, timeout=1):                   # тот же файл под другим именем: вход того же потока
                pass

    odd = F.FileLock(tmp / "odd.lock", info={"what": "имя \udc80 и путь", "where": tmp})
    with odd:
        assert F.is_locked(tmp / "odd.lock")
    real = F._try_lock

    def unsupported(fd):
        raise F.LockUnsupported(95, "нет замков")
    flaky = F.FileLock(tmp / "flaky.lock", optional=True)
    F._try_lock = unsupported
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            assert flaky.acquire() and flaky.degraded
    finally:
        F._try_lock = real
    flaky.release()
    assert flaky.acquire() and F.is_locked(tmp / "flaky.lock"), "после сбоя замков настоящий замок не взят"
    flaky.release()
    assert not F.is_locked(tmp / "flaky.lock"), "настоящий замок не отпущен: пометка «без замка» залипла"

    fd = os.open(tmp / "gone.lock", os.O_RDWR | os.O_CREAT)
    try:
        assert F._replaced(fd, str(tmp / "gone.lock")) is False
        if os.name != "nt":
            os.unlink(tmp / "gone.lock")
            assert F._replaced(fd, str(tmp / "gone.lock")) is True, "убранный файл замка не замечен"
    finally:
        os.close(fd)


@test
def test_file_lock_timeout_is_not_swallowed_as_a_disk_error(tmp: Path):
    """`LockTimeout` — не `OSError`: повсюду в движке `except OSError: pass` означает отказ диска, и таймаут замка нельзя
    молча проглотить им вместе с записью, которую замок защищает; владелец назван в сообщении."""
    F = _fs()
    assert not issubclass(F.LockTimeout, OSError)
    path = tmp / "w.lock"
    with F.FileLock(path, info={"what": "ночной разбор"}):
        res = {}

        def other():
            try:
                with F.FileLock(path, timeout=0.2):
                    pass
            except OSError:
                res["swallowed"] = True
            except F.LockTimeout as e:
                res["owner"] = e.owner
        t = threading.Thread(target=other)
        t.start()
        t.join(30)
    assert "swallowed" not in res and "ночной разбор" in res.get("owner", ""), res


@test
def test_file_lock_is_not_inherited_by_a_forked_child(tmp: Path):
    """Дочерний процесс после `fork` замка родителя не получает и не снимает: родитель держит его до конца."""
    if not hasattr(os, "fork"):
        return
    _fs()
    cp = subprocess.run([sys.executable, "-c", _FORK, str(SCRIPTS), str(tmp / "f.lock")],
                        capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0 and cp.stdout.split() == ["child_blocked", "held"], (cp.stdout, cp.stderr[-400:])


@test
def test_read_state_quarantines_a_damaged_file_loudly(tmp: Path):
    """Нет файла — значение по умолчанию молча; битый или не того типа — переносится в `.corrupt`, строка в stderr."""
    F = _fs()
    p = tmp / "state.json"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert F.read_state(p, {"a": 1}) == {"a": 1}
    assert err.getvalue() == "" and not list(tmp.iterdir()), "отсутствие файла не повод шуметь"
    F.write_json(p, {"ok": True})
    assert F.read_state(p, {}) == {"ok": True}
    p.write_text('{"cut": ', encoding="utf-8")
    F._WARNED.clear()
    with contextlib.redirect_stderr(err):
        assert F.read_state(p, {"d": 1}) == {"d": 1}
    assert "state.json" in err.getvalue() and ".corrupt" in err.getvalue(), err.getvalue()
    assert not p.exists() and (tmp / "state.json.corrupt").read_text(encoding="utf-8") == '{"cut": ', list(tmp.iterdir())
    p.write_text("[1, 2]", encoding="utf-8")                # допустимый JSON не того типа: состояние — словарь
    with contextlib.redirect_stderr(io.StringIO()):
        assert F.read_state(p, {}) == {}
    assert (tmp / "state.json.corrupt-2").is_file() and (tmp / "state.json.corrupt").is_file(), \
        "второе повреждение затёрло первый след"
    p.write_text("[1, 2]", encoding="utf-8")
    assert F.read_state(p, None) == [1, 2], "без ожидаемого типа любой JSON принимается"
    assert F.read_state(p, [], quarantine=False) == [1, 2]


@test
def test_read_state_leaves_a_busy_file_in_place(tmp: Path):
    """Файл, который не удалось открыть (доступ, занятость Windows), — не повреждение: он остаётся на месте."""
    F = _fs()
    p = tmp / "busy.json"
    F.write_json(p, {"x": 1})
    real_open = open

    def denied(path, *a, **kw):
        if str(path) == str(p):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)
    F.open = denied                                           # имя модуля перекрывает builtins.open
    try:
        assert F.read_state(p, {"d": 1}) == {"d": 1}
    finally:
        del F.open
    assert p.is_file() and not (tmp / "busy.json.corrupt").exists(), sorted(x.name for x in tmp.iterdir())


@test
def test_sweep_tmp_removes_only_old_orphans(tmp: Path):
    """Уборка: осиротевшие `*.aurora-tmp` старше срока уходят, свежие (чужая запись идёт) и остальные имена остаются."""
    F = _fs()
    old, fresh, other, legacy = (tmp / ".1-2-3.aurora-tmp", tmp / ".4-5-6.aurora-tmp", tmp / "keep.json",
                                 tmp / "meta.json.tmp")
    for p in (old, fresh, other, legacy):
        p.write_text("x", encoding="utf-8")
    past = time.time() - 7200
    for p in (old, other, legacy):
        os.utime(p, (past, past))
    assert F.sweep_tmp(tmp) == [old.name]
    assert not old.exists() and fresh.exists() and other.exists() and legacy.exists()
    assert F.sweep_tmp(tmp, patterns=("*.aurora-tmp", "*.json.tmp")) == [legacy.name]
    assert F.sweep_tmp(tmp / "нет-такой") == []


@test
def test_aurora_common_delegates_to_the_core_and_finds_it_by_path(tmp: Path):
    """`replace_file` и `write_text_atomic` остались в `aurora_common` (на них ссылаются все скрипты) и работают через
    ядро — в том числе когда `aurora_common` загружен по пути файла и его папки нет в `sys.path`."""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('ac', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "assert sys.argv[2] not in sys.path\n"
        "m.write_text_atomic(sys.argv[3], 'через ядро\\n')\n"
        "open(sys.argv[3] + '.src', 'w').write('новый')\n"
        "m.replace_file(sys.argv[3] + '.src', sys.argv[3])\n"
        "print(open(sys.argv[3], encoding='utf-8').read())\n")
    target = tmp / "out.txt"
    cp = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPTS / "aurora_common.py"), str(SCRIPTS), str(target)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(tmp), timeout=120)
    assert cp.returncode == 0 and cp.stdout.strip() == "новый", (cp.stdout, cp.stderr[-500:])
    assert not _leftovers(tmp), _leftovers(tmp)


@test
def test_core_is_a_leaf_module_that_the_manifest_ships_before_aurora_common(tmp: Path):
    """Ядро: только стандартная библиотека, без `:=` и `match` (читается старым Python), в манифесте — раньше
    `aurora_common`: обрыв обновления между записями не оставит новый `aurora_common` без него."""
    src = (SCRIPTS / "aurora_fs.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    std = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else None
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add((node.module or "").split(".")[0])
        assert not isinstance(node, ast.NamedExpr), "морж в ядре: старый Python его не прочтёт"
        assert not isinstance(node, getattr(ast, "Match", ())), "match в ядре: старый Python его не прочтёт"
    if std is not None:
        assert names <= std, f"ядро тянет не стандартное: {sorted(names - std)}"
    assert not (names & {"aurora_common", "card_types", "agent_core"}), names
    manifest = [ln.split("=>")[0].strip() for ln in (KIT / "engine_manifest.txt").read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#") and "=>" in ln]
    assert "scripts/aurora_fs.py" in manifest, "ядро не доставляется в проекты"
    assert manifest.index("scripts/aurora_fs.py") < manifest.index("scripts/aurora_common.py"), manifest[:6]
