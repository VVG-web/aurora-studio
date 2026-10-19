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
