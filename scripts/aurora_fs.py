#!/usr/bin/env python3
"""aurora_fs.py — ядро надёжности записи: атомарный файл, замок ОС, чтение состояния (BL-SEC-25).

Один класс дефектов жил в десятке подсистем по-своему: запись «временный файл + подмена» с
фиксированным именем (два писателя делят его), без `fsync` (после сбоя питания на месте файла нуль
байт), замки в пяти несовместимых форматах «pid в файле» (мёртвый держатель держит замок до
таймаута), «битый файл → пусто» в десятках мест (недописанное состояние молча забывалось).
Здесь это сделано один раз, на стандартной библиотеке:

  write_atomic(path, data, …)   файл целиком или никак: уникальный временный файл рядом, `fsync`,
                                подмена с повтором на Windows, права и перевод строк по параметрам
  replace_file(src, dst)        `os.replace` с повтором на Windows (занятый читателем файл)
  FileLock(path)                замок ОС (`flock` / `msvcrt.locking`): умирает вместе с процессом,
                                поэтому «брошенного» замка не бывает; повторный вход того же потока допустим
  read_state(path, default)     JSON-состояние; повреждённый файл переносится в `<имя>.corrupt` и
                                называется вслух, а не молча заменяется пустотой
  sweep_tmp(folder)             уборка осиротевших временных файлов (`*.aurora-tmp`) старше срока

Модуль листовой: импортирует только стандартную библиотеку — его можно подключать откуда угодно, и в
манифесте движка он стоит раньше `aurora_common.py` (обрыв обновления между записями не оставит
`aurora_common` без него). Синтаксис — без `:=` и `match`: читается и старым Python.
"""
from __future__ import annotations

import fnmatch
import itertools
import json
import os
import stat
import sys
import threading
import time

TMP_SUFFIX = ".aurora-tmp"                # так называются временные файлы записи; `.gitignore` проекта их закрывает
_SEQ = itertools.count(1)
_WARNED: set = set()                      # о чём уже сказано в этом процессе (по ключу): повторять не нужно


def _warn_once(key: str, text: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        print(text, file=sys.stderr)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- подмена и запись

def replace_file(src, dst, *, windows: "bool | None" = None, attempts: int = 40, pause: float = 0.025) -> None:
    """`os.replace` с повтором на Windows: файл, который сейчас читают, туда не подменить.

    На POSIX подмена не зависит от того, открыт ли файл у других: читатель дочитывает старый, новый встаёт
    на место. На Windows файл, открытый другим потоком или процессом (или только что удалённый и ещё не
    отпущенный), не поддаётся — `PermissionError` (WinError 5 или 32), хотя прав хватает. Занятость проходит
    за миллисекунды: ждём и пробуем снова. На другой системе `PermissionError` — настоящий отказ в правах,
    и он поднимается сразу.
    """
    retry = (os.name == "nt") if windows is None else windows
    for n in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not retry or n == attempts - 1:
                raise
            time.sleep(pause * min(n + 1, 8))


def _encode(text: str, encoding: str) -> bytes:
    """Текст → байты. Непарный суррогат (след непонятных байтов исходника) не роняет запись всего прогона."""
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        pass
    try:
        return text.encode(encoding, "surrogateescape")
    except UnicodeEncodeError:
        return text.encode(encoding, "replace")


def crlf_file(path: str) -> bool:
    """Файл на диске записан с `\\r\\n` (в большинстве строк)? Новый файл и файл с `\\n` — нет."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(65536)
    except OSError:
        return False
    return raw.count(b"\r\n") * 2 > raw.count(b"\n")


def _payload(data, encoding: str, newline: "str | None", target: str) -> bytes:
    """Байты к записи. `newline` как у `open()`: `None` — перевод строки платформы, `""`/`"\\n"` — как есть,
    `"\\r\\n"` — принудительно CRLF, `"keep"` — как у файла на диске (CRLF, если он уже такой, иначе LF)."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    text = str(data)
    if newline == "keep":
        eol = "\r\n" if crlf_file(target) else "\n"
    elif newline is None:
        eol = os.linesep
    elif newline in ("", "\n"):
        eol = ""
    elif newline == "\r\n":
        eol = newline
    else:
        raise ValueError("write_atomic: newline %r не поддерживается" % (newline,))
    if eol == "\r\n":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    elif eol == "\n":
        text = text.replace("\r\n", "\n")
    return _encode(text, encoding)


def _fsync_dir(folder: str) -> None:
    """Запись каталога на диск (имя нового файла): только POSIX, остальное — по возможности."""
    if os.name == "nt" or not hasattr(os, "O_RDONLY"):
        return
    try:
        fd = os.open(folder or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path, data, *, mode: "int | None" = None, newline: "str | None" = "", encoding: str = "utf-8",
                 fsync: bool = True, follow_links: bool = True) -> None:
    """Записать файл целиком или не трогать его: временный файл рядом, `fsync`, подмена.

    Прямая запись обрывается посреди файла вместе с процессом (убитый `update`, закрытая крышка, диск полон) —
    и обрезок остаётся настоящим файлом проекта. Здесь на месте остаётся либо прежний файл, либо новый.
    Имя временного файла — своё у процесса, потока и каждой записи (и короткое: к длинному имени карточки
    ничего не прибавляется), поэтому два писателя в один файл не делят его. Прерывание (`KeyboardInterrupt`,
    сбой диска) убирает временный файл.

    `data` — текст или байты. `newline`: `""` (по умолчанию) — текст как есть на любой системе; `None` —
    перевод строки платформы; `"\\r\\n"` — CRLF; `"keep"` — как в файле на диске. `mode` — права нового файла
    (секрет: `0o600`, файл рождается уже с ними, окна с широкими правами нет); без него файл наследует права
    прежнего. `follow_links=True`: ссылка не заменяется — пишется файл, на который она ведёт; `False` —
    заменяется сама ссылка. `fsync=False` — для массовых записей, где важнее скорость.
    """
    target = os.path.realpath(path) if follow_links else os.path.abspath(path)
    folder = os.path.dirname(target)
    if folder:
        os.makedirs(folder, exist_ok=True)
    payload = _payload(data, encoding, newline, target)
    try:
        old_mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        old_mode = None                     # прежнего файла нет
    tmp = os.path.join(folder, ".%d-%d-%d%s" % (os.getpid(), threading.get_ident(), next(_SEQ), TMP_SUFFIX))
    start = mode if mode is not None else (old_mode if old_mode is not None else 0o666)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), start)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        want = mode if mode is not None else old_mode
        if want is not None:
            try:
                os.chmod(tmp, want)         # umask не должен урезать права прежнего файла
            except OSError:
                pass
        replace_file(tmp, target)
        tmp = ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if fsync:
        _fsync_dir(folder)


def write_json(path, obj, *, indent: "int | None" = 2, **kw) -> None:
    """Записать JSON-состояние (`write_atomic`), русские буквы как есть, перевод строки в конце."""
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=indent) + "\n", **kw)


# --------------------------------------------------------------------------- чтение состояния

def _quarantine(path: str) -> str:
    """Перенести повреждённый файл в `<имя>.corrupt` (занято — `.corrupt-2`, `-3`). → новый путь или пусто."""
    for n in range(1, 50):
        dest = path + (".corrupt" if n == 1 else ".corrupt-%d" % n)
        if os.path.exists(dest):
            continue
        try:
            os.rename(path, dest)
            return dest
        except OSError:
            return ""
    return ""


def read_state(path, default=None, *, kind: "type | None" = None, quarantine: bool = True):
    """Прочитать JSON-состояние. Нет файла — `default` молча; не читается — `default`, но не молча.

    Файл, который есть, но не разобрался (обрыв записи старым кодом, ручная правка) или несёт не тот тип
    (`kind`, по умолчанию — тип `default`), не «пуст»: его содержимое — след сбоя. Он переносится рядом в
    `<имя>.corrupt` (прежний `.corrupt` не затирается), на stderr — одна строка, и вызывающий получает `default`.
    Перед переносом файл перечитывается ещё раз: писатель прежнего кода мог быть на середине записи.
    Ошибка доступа (`PermissionError`, занятый файл Windows) — не повреждение: файл остаётся на месте.
    """
    path = os.fspath(path)
    want = kind if kind is not None else (type(default) if default is not None else None)
    bad = ""
    for attempt in (0, 1):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return default
        except OSError:
            return default
        except (ValueError, UnicodeDecodeError) as e:
            bad = "не разобрался (%s)" % str(e)[:80]
        else:
            if want is None or isinstance(data, want):
                return data
            bad = "содержит %s вместо %s" % (type(data).__name__, want.__name__)
        if attempt == 0:
            time.sleep(0.05)
    if quarantine:
        dest = _quarantine(path)
        _warn_once("corrupt:" + path, "aurora_fs: файл состояния %s %s — %s; берётся значение по умолчанию"
                   % (os.path.basename(path), bad, ("перенесён в " + os.path.basename(dest)) if dest else "оставлен на месте"))
    return default


# --------------------------------------------------------------------------- уборка

def sweep_tmp(folder, *, patterns: tuple = ("*" + TMP_SUFFIX,), older_than: float = 3600.0) -> list:
    """Убрать из каталога (не рекурсивно) осиротевшие временные файлы старше срока. → удалённые имена.

    Запись занимает миллисекунды: временный файл старше часа — след убитого процесса. Свежие не трогаются —
    рядом может идти чужая запись. `patterns` — маски имён (прежние временные имена: `("*.aurora-tmp", "*.json.tmp")`).
    """
    out = []
    try:
        names = os.listdir(str(folder))
    except OSError:
        return out
    now = time.time()
    for name in names:
        if not any(fnmatch.fnmatch(name, p) for p in patterns):
            continue
        full = os.path.join(str(folder), name)
        try:
            if not os.path.isfile(full) or os.path.islink(full) or now - os.path.getmtime(full) < older_than:
                continue
            os.unlink(full)
            out.append(name)
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------- замок ОС

class LockTimeout(OSError):
    """Замок не достался за отведённое время (держит другой процесс или поток)."""


class LockUnsupported(OSError):
    """Файловая система не умеет замки ОС (часть сетевых папок): заперто быть не может, отказ — по запросу."""


# Windows: запирается один байт далеко за концом файла, а не начало — иначе описание владельца в начале файла
# не читалось бы другим процессом (запертый диапазон читать нельзя), а это нужно, чтобы назвать держателя.
_LOCK_POS = 1 << 30
_HELD: dict = {}                          # ключ замка → {"tid", "n", "fd"}: замок, взятый этим процессом
_HELD_GUARD = threading.Lock()
_BUSY_ERRNO = (11, 13, 35, 36)            # EAGAIN, EACCES, EWOULDBLOCK (macOS), EDEADLK / EDEADLOCK (Windows)


def _lock_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _try_lock(fd: int) -> bool:
    """Одна неблокирующая попытка. True — замок взят; False — занят; `LockUnsupported` — ОС не умеет."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _LOCK_POS, 0)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _BUSY_ERRNO:
            return False
        raise LockUnsupported(e.errno, "замки файлов не поддерживаются: %s" % e)
    return True


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _LOCK_POS, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass                              # закрытие дескриптора снимет замок в любом случае


class FileLock:
    """Межпроцессный замок на файле: `with FileLock(путь): …`.

    Замок принадлежит ОС, а не файлу: он снимается вместе с процессом (даже убитым), поэтому `kill -9`, закрытая
    крышка и сбой питания не оставляют «брошенного» замка и не нужны ни проверка «жив ли pid», ни таймаут давности.
    Файл замка остаётся на диске всегда (удалять его при работе нельзя: ожидающий держит его старый inode, и новый
    файл дал бы второго хозяина); внутри — описание владельца (`pid`, время, `info`), пока замок взят.
    Поток, уже державший замок, входит повторно (счётчик): вложенные вызовы под одним замком не блокируют сами себя;
    другой поток того же процесса ждёт, как чужой процесс.

    `timeout` — сколько ждать в `with` (секунды; `None` — без предела); не дождались — `LockTimeout` (это `OSError`).
    `optional=True`: на файловой системе без замков ОС работа идёт без замка (одно предупреждение), а не отказом.
    """

    def __init__(self, path, *, timeout: "float | None" = 60.0, poll: float = 0.05, info: "dict | None" = None,
                 optional: bool = False):
        self.path = os.fspath(path)
        self.timeout, self.poll, self.info, self.optional = timeout, poll, info, optional
        self.degraded = False
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self, blocking: bool = True, timeout: "float | None" = None) -> bool:
        """Взять замок. → True взят; False — не дождались (неблокирующий вызов или истёк `timeout`)."""
        if self._held:
            return True
        key, me = _lock_key(self.path), threading.get_ident()
        with _HELD_GUARD:
            mine = _HELD.get(key)
            if mine is not None and mine["tid"] == me:
                mine["n"] += 1
                self._held = True
                return True
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
        wait = timeout if timeout is not None else self.timeout
        deadline = None if wait is None else time.monotonic() + wait
        got = False
        try:
            while True:
                try:
                    got = _try_lock(fd)
                except LockUnsupported as e:
                    if not self.optional:
                        raise
                    _warn_once("nolock:" + key, "aurora_fs: на этой файловой системе нет замков ОС (%s) — "
                               "работа идёт без замка: %s" % (e, self.path))
                    os.close(fd)
                    self.degraded = self._held = True
                    return True
                if got:
                    break
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
                    return False
                time.sleep(self.poll if deadline is None else max(0.0, min(self.poll, deadline - time.monotonic())))
            self._describe(fd)
            with _HELD_GUARD:
                _HELD[key] = {"tid": me, "n": 1, "fd": fd}
            self._held = True
            return True
        finally:
            if not self._held:
                if got:
                    _unlock(fd)
                os.close(fd)

    def _describe(self, fd: int) -> None:
        rec = {"pid": os.getpid(), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        rec.update(self.info or {})
        try:
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(rec, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass                          # описание — подсказка человеку, замок держится и без него

    def release(self) -> bool:
        """Отпустить замок (вложенный вход — уменьшить счётчик). → отпущен ли замок насовсем."""
        if not self._held:
            return False
        self._held = False
        if self.degraded:
            return True
        key = _lock_key(self.path)
        with _HELD_GUARD:
            mine = _HELD.get(key)
            if mine is None:
                return False
            mine["n"] -= 1
            if mine["n"] > 0:
                return False
            del _HELD[key]
        fd = mine["fd"]
        try:
            os.ftruncate(fd, 0)           # пустой файл — «никто не держит»
        except OSError:
            pass
        _unlock(fd)
        os.close(fd)
        return True

    def __enter__(self) -> "FileLock":
        if not self.acquire(timeout=self.timeout):
            raise LockTimeout("замок %s занят: %s" % (self.path, describe_owner(self.path) or "владелец неизвестен"))
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def describe_owner(path) -> str:
    """Кто держит замок, строкой для сообщения; пусто — не держит или описание не читается."""
    try:
        with open(os.fspath(path), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(rec, dict) or not rec:
        return ""
    who = str(rec.get("what") or rec.get("task") or "").strip()
    return ("процесс %s" % rec.get("pid", "?")) + (", с %s" % rec["at"] if rec.get("at") else "") + (" — " + who if who else "")


def is_locked(path) -> bool:
    """Держит ли замок кто-то сейчас (этот или другой процесс). Не создаёт файла; ничего не занимает надолго."""
    path = os.fspath(path)
    if not os.path.exists(path):
        return False
    with _HELD_GUARD:
        if _lock_key(path) in _HELD:
            return True
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    except LockUnsupported:
        return False
    finally:
        os.close(fd)
