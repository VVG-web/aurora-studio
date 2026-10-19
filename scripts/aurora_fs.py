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

import errno
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


def utc_stamp() -> str:
    """Время в UTC строкой ISO («2026-10-19T08:00:00Z»): движок записывает время только в UTC (оно показывается в местном)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _encode(text: str, encoding: str, strict: bool = False) -> bytes:
    """Текст → байты. Непарный суррогат (след непонятных байтов исходника) не роняет запись всего прогона, если не
    `strict`: тогда это `UnicodeEncodeError`, как у `Path.write_text`."""
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        if strict:
            raise
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


def _payload(data, encoding: str, newline: "str | None", target: str, strict: bool = False) -> bytes:
    """Байты к записи. `newline` как у `open()`: `None` — перевод строки платформы, `""`/`"\\n"` — как есть,
    `"\\r\\n"` — принудительно CRLF, `"keep"` — как у файла на диске (CRLF, если он уже такой, иначе LF)."""
    if isinstance(data, (bytes, bytearray)):
        if strict:
            raise TypeError("write_atomic: ожидался текст, а пришли байты")
        return bytes(data)
    if strict and not isinstance(data, str):
        raise TypeError("write_atomic: ожидался текст, а пришёл %s" % type(data).__name__)
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
    return _encode(text, encoding, strict)


_NO_FSYNC = tuple(getattr(errno, n) for n in ("EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP") if hasattr(errno, n))


def _fsync(fd: int) -> None:
    """`fsync` файла; файловая система, которая его не умеет (часть FUSE и сетевых папок), записи не мешает.
    Настоящий сбой диска (`EIO`, `ENOSPC`) поднимается."""
    try:
        os.fsync(fd)
    except OSError as e:
        if e.errno not in _NO_FSYNC:
            raise


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
                 fsync: bool = True, follow_links: bool = True, strict: bool = False, makedirs: bool = True) -> None:
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
    заменяется сама ссылка. `fsync=False` — для массовых записей, где важнее скорость. `strict=True` — только текст
    и без `surrogateescape`: непарный суррогат — `UnicodeEncodeError`, как у `Path.write_text`. `makedirs=False` —
    каталога нет: `FileNotFoundError`, а не молчаливое создание.
    """
    target = os.path.realpath(path) if follow_links else os.path.abspath(path)
    folder = os.path.dirname(target)
    if folder and makedirs:
        os.makedirs(folder, exist_ok=True)
    payload = _payload(data, encoding, newline, target, strict)
    try:
        old_mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        old_mode = None                     # прежнего файла нет
    start = mode if mode is not None else (old_mode if old_mode is not None else 0o666)
    for _ in range(100):                    # имя занято следом убитого процесса с тем же pid и потоком — берём следующее
        tmp = os.path.join(folder, ".%d-%d-%d%s" % (os.getpid(), threading.get_ident(), next(_SEQ), TMP_SUFFIX))
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), start)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("write_atomic: не удалось подобрать имя временного файла в %s" % folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            if fsync:
                _fsync(fh.fileno())
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
    """Записать JSON-состояние (`write_atomic`), русские буквы как есть, перевод строки в конце. Непарный суррогат в
    данных (имя файла из непонятных байтов) уходит escape-кодом: файл остаётся читаемым, а не «битым UTF-8»."""
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(obj, ensure_ascii=True, indent=indent)
    write_atomic(path, text + "\n", **kw)


# --------------------------------------------------------------------------- чтение состояния

MAX_STATE_BYTES = 64 << 20     # файл состояния больше — не состояние, а мусор: не читаем целиком
QUARANTINE_AGE = 2.0          # файл моложе (секунд) в карантин не уходит: его может дописывать писатель прежнего кода


def _stat_sig(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _quarantine(path: str, sig) -> str:
    """Перенести повреждённый файл в `<имя>.corrupt` (занято — `.corrupt-2`, `-3`), если он всё тот же (`sig` — inode,
    размер и время изменения при чтении): другой читатель мог уже убрать битый файл, а писатель — положить годный.
    → новый путь или пусто."""
    if _stat_sig(path) != sig:
        return ""
    for n in range(1, 50):
        dest = path + (".corrupt" if n == 1 else ".corrupt-%d" % n)
        if os.path.exists(dest):
            continue
        try:
            os.link(path, dest)              # не затирает уже существующий .corrupt (rename на POSIX затёр бы)
        except FileExistsError:
            continue
        except (OSError, NotImplementedError, AttributeError):
            try:
                os.rename(path, dest)        # файловая система без жёстких ссылок
                return dest
            except OSError:
                return ""
        try:
            os.unlink(path)
        except OSError:
            try:
                os.unlink(dest)              # исходный не убрался (Windows: занят) — копию не оставляем, повтор будет позже
            except OSError:
                pass
            return ""
        return dest
    return ""


def _json_type(default, kind):
    """Какого типа должен быть файл: `kind`, а нет его — базовый тип `default` (словарь любого вида — `dict`)."""
    if kind is not None:
        return kind
    for base in (dict, list, str, bool, int, float):
        if isinstance(default, base):
            return base
    return None


def _try_read(path: str, want):
    """Одно чтение: ("ok", данные) | ("missing", None) | ("bad", причина) | ("busy", исключение)."""
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return "busy", OSError("%s — не обычный файл" % path)        # FIFO или устройство: не открываем, не ждём вечно
        if st.st_size > MAX_STATE_BYTES:
            return "bad", "слишком большой (%d байт)" % st.st_size
        with open(path, encoding="utf-8-sig") as fh:          # BOM (Блокнот, PowerShell) — не повреждение
            data = json.load(fh)
    except FileNotFoundError:
        return "missing", None
    except (ValueError, RecursionError) as e:                 # JSONDecodeError, UnicodeDecodeError, слишком глубокая вложенность
        return "bad", "не разобрался (%s)" % str(e)[:80]
    except OSError as e:
        return "busy", e
    if want is None or isinstance(data, want):
        return "ok", data
    return "bad", "содержит %s вместо %s" % (type(data).__name__, want.__name__)


def read_state(path, default=None, *, kind: "type | None" = None, quarantine: bool = True, strict: bool = False):
    """Прочитать JSON-состояние. Нет файла — `default` молча; не читается — `default`, но не молча.

    Файл, который есть, но не разобрался (обрыв записи старым кодом, ручная правка) или несёт не тот тип
    (`kind`, по умолчанию — тип `default`), не «пуст»: его содержимое — след сбоя. Он переносится рядом в
    `<имя>.corrupt` (прежний `.corrupt` не затирается), на stderr — одна строка, и вызывающий получает `default`.
    Перенос осторожный: файл перечитывается через паузу, должен остаться тем же (inode, размер, время) и быть старше
    `QUARANTINE_AGE` секунд — писатель прежнего кода мог быть на середине записи, а другой читатель уже мог всё убрать.
    Ошибка доступа (`PermissionError`, занятый файл Windows) — не повреждение: чтение повторяется несколько раз, файл
    остаётся на месте. `strict=True` — для «прочитать-изменить-записать»: недоступный файл поднимает ошибку, а не
    подсовывает `default` (иначе запись поверх затёрла бы всё, чего не удалось прочитать).
    """
    path = os.fspath(path)
    want = _json_type(default, kind)
    state, val = "bad", ""
    for n in range(5):
        state, val = _try_read(path, want)
        if state == "busy" and n < 4:
            time.sleep(0.03)
            continue
        break
    if state == "ok":
        return val
    if state == "busy":
        if strict:
            raise val
        return default
    if state == "missing":
        return default
    first = _stat_sig(path)
    time.sleep(0.05)
    state, again = _try_read(path, want)
    if state == "ok":
        return again
    if state == "busy" and strict:
        raise again
    if state != "bad":
        return default
    second = _stat_sig(path)
    if quarantine and first is not None and first == second and time.time() - second[2] / 1e9 >= QUARANTINE_AGE:
        dest = _quarantine(path, second)
        _warn_once("corrupt:" + (dest or path), "aurora_fs: файл состояния %s %s — %s; берётся значение по умолчанию"
                   % (os.path.basename(path), again, ("перенесён в " + os.path.basename(dest)) if dest else "оставлен на месте"))
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


def sweep_tree(root, *, patterns: tuple = ("*" + TMP_SUFFIX,), older_than: float = 3600.0, skip: tuple = (".git",)) -> list:
    """`sweep_tmp` по дереву: осиротевшие временные файлы во всех подкаталогах `root` (карточки базы пишутся в разделы).
    Ссылки не обходятся, `skip` — имена каталогов, куда не заходим. → относительные пути удалённых."""
    out = []
    base = str(root)
    for folder, dirs, _files in os.walk(base, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in sweep_tmp(folder, patterns=patterns, older_than=older_than):
            out.append(os.path.relpath(os.path.join(folder, name), base).replace("\\", "/"))
    return out


# --------------------------------------------------------------------------- замок ОС

class LockTimeout(Exception):
    """Замок не достался за отведённое время (держит другой процесс или поток). Не `OSError`: повсюду в движке
    `except OSError: pass` — отказ диска, и молча проглотить им таймаут замка значило бы потерять запись, которую замок
    и должен защищать. `owner` — кто держит (строка для человека), `path` — файл замка."""

    def __init__(self, path: str, owner: str = ""):
        self.path, self.owner = str(path), str(owner)
        super().__init__("замок %s занят: %s" % (self.path, self.owner or "владелец неизвестен"))


class LockUnsupported(OSError):
    """Файловая система не умеет замки ОС (часть сетевых папок): заперто быть не может, отказ — по запросу."""


# Windows: запирается один байт далеко за концом файла, а не начало — иначе описание владельца в начале файла
# не читалось бы другим процессом (запертый диапазон читать нельзя), а это нужно, чтобы назвать держателя.
_LOCK_POS = 1 << 30
_HELD: dict = {}                          # (устройство, inode) файла замка → {"tid", "n", "fd"}: замок этого процесса
_HELD_GUARD = threading.RLock()           # RLock: обработчик сигнала может войти в замок поверх прерванного
_BUSY = (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK))
_NO_LOCKS = tuple(getattr(errno, n) for n in ("ENOLCK", "EOPNOTSUPP", "ENOTSUP", "EINVAL", "ENOSYS") if hasattr(errno, n))


def _try_lock(fd: int) -> bool:
    """Одна неблокирующая попытка. True — замок взят; False — занят; `LockUnsupported` — ОС замков не умеет.
    Любая другая ошибка (диск, дескриптор) поднимается как есть: «не поддерживается» — только по ясным признакам."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _LOCK_POS, 0)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in _BUSY:
                return False
            raise LockUnsupported(e.errno, "замки файлов не поддерживаются: %s" % e)
        return True
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _BUSY:
            return False
        if e.errno in _NO_LOCKS:
            raise LockUnsupported(e.errno, "замки файлов не поддерживаются: %s" % e)
        raise
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


def _open_lock_file(path: str, create: bool):
    """Открыть файл замка. Ссылка не разворачивается (`O_NOFOLLOW`: подложенная ссылка на чужой файл не будет усечена
    записью описания владельца) и это обязан быть обычный файл; создаётся с правами 0600."""
    if os.path.islink(path):                # Windows без O_NOFOLLOW: ссылку отсекаем заранее
        raise OSError("файл замка %s — ссылка" % path)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | (os.O_CREAT if create else 0)
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("файл замка %s — не обычный файл" % path)
    return fd


def _file_id(fd: int, path: str) -> tuple:
    """Кто есть файл замка: (устройство, inode) открытого дескриптора — одно и то же при любых написаниях пути (регистр,
    жёсткая ссылка, смена рабочей папки). Файловая система без inode — по настоящему пути."""
    st = os.fstat(fd)
    if st.st_ino:
        return (st.st_dev, st.st_ino)
    return (os.path.normcase(os.path.realpath(path)),)


def _replaced(fd: int, path: str) -> bool:
    """Файл замка убрали или подменили, пока мы его запирали (`rm`, `git clean`)? Тогда наш замок — на файле, который уже
    никто не откроет по имени, и второй процесс взял бы «тот же» замок на новом. POSIX: у Windows открытый файл не удалить."""
    if os.name == "nt":
        return False
    try:
        now = os.stat(path)
    except OSError:
        return True
    st = os.fstat(fd)
    return (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino)


class FileLock:
    """Межпроцессный замок на файле: `with FileLock(путь): …`.

    Замок принадлежит ОС, а не файлу: он снимается вместе с процессом (даже убитым), поэтому `kill -9`, закрытая
    крышка и сбой питания не оставляют «брошенного» замка и не нужны ни проверка «жив ли pid», ни таймаут давности.
    Это верно для локальных файловых систем; сетевая папка (NFS без lockd, SMB с `nobrl`) или синхронизируемая
    (OneDrive, Dropbox, iCloud) замок между компьютерами не даёт, а на Windows снятие после смерти процесса может
    занять заметное время. Файл замка остаётся на диске всегда (удалять его при работе нельзя: ожидающий держит
    его старый inode, и новый файл дал бы второго хозяина); внутри — описание владельца (`pid`, время, `info`), пока
    замок взят: читать его имеет смысл, только убедившись через `is_locked`, что замок кем-то держится.
    Поток, уже державший замок, входит повторно (счётчик на файл, а не на объект): вложенные вызовы под одним замком не
    блокируют сами себя; другой поток того же процесса — даже с тем же объектом — ждёт, как чужой процесс. Замок не
    переживает `fork` в дочернем процессе: унаследованные дескрипторы закрываются, держатель остаётся родитель.
    Ожидание — опрос раз в `poll` секунд без очереди: держатель, тут же берущий замок снова, может обгонять ожидающих.

    `timeout` — сколько ждать в `with` (секунды; `None` — без предела); не дождались — `LockTimeout`.
    `optional=True`: на файловой системе без замков ОС работа идёт без замка (одно предупреждение), а не отказом.
    """

    def __init__(self, path, *, timeout: "float | None" = 60.0, poll: float = 0.05, info: "dict | None" = None,
                 optional: bool = False):
        self.path = os.path.abspath(os.fspath(path))
        self.timeout, self.poll, self.info, self.optional = timeout, poll, info, optional
        self.degraded = False
        self._taken: list = []            # стек взятий этим объектом: ключ файла или None (работа без замка)

    @property
    def held(self) -> bool:
        return bool(self._taken)

    def acquire(self, blocking: bool = True, timeout: "float | None" = None) -> bool:
        """Взять замок. → True взят; False — не дождались (неблокирующий вызов или истёк `timeout`)."""
        me = threading.get_ident()
        wait = timeout if timeout is not None else self.timeout
        deadline = None if wait is None else time.monotonic() + wait
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(8):                # файл замка могли убрать, пока мы его запирали: открываем заново
            fd = _open_lock_file(self.path, create=True)
            key, got, done = _file_id(fd, self.path), False, False
            try:
                with _HELD_GUARD:
                    mine = _HELD.get(key)
                    if mine is not None and mine["tid"] == me:
                        mine["n"] += 1
                        self._taken.append(key)
                        done = True
                        return True       # вход того же потока; лишний дескриптор закроет finally
                while True:
                    try:
                        got = _try_lock(fd)
                    except LockUnsupported as e:
                        if not self.optional:
                            raise
                        _warn_once("nolock:" + self.path, "aurora_fs: на этой файловой системе нет замков ОС (%s) — "
                                   "работа идёт без замка: %s" % (e, self.path))
                        self.degraded = True
                        self._taken.append(None)
                        done = True
                        return True
                    if got:
                        break
                    if not blocking or (deadline is not None and time.monotonic() >= deadline):
                        return False
                    time.sleep(self.poll if deadline is None else max(0.0, min(self.poll, deadline - time.monotonic())))
                if _replaced(fd, self.path):
                    continue              # замок взят на файле, которого по имени больше нет: заново
                self._describe(fd)
                with _HELD_GUARD:
                    _HELD[key] = {"tid": me, "n": 1, "fd": fd}
                    self._taken.append(key)
                    done = True
                return True
            finally:
                if not done:
                    if got:
                        _unlock(fd)
                    os.close(fd)
                elif _HELD.get(key, {}).get("fd") != fd:
                    os.close(fd)          # вход того же потока или работа без замка: дескриптор не нужен
        raise OSError("не удалось запереть %s: файл замка постоянно убирают" % self.path)

    def _describe(self, fd: int) -> None:
        rec = {"pid": os.getpid(), "at": utc_stamp()}
        rec.update(self.info or {})
        try:
            data = json.dumps(rec, ensure_ascii=False, default=str).encode("utf-8", "replace")
            os.lseek(fd, 0, 0)
            os.ftruncate(fd, 0)
            os.write(fd, data)
        except (OSError, ValueError, TypeError):
            pass                          # описание — подсказка человеку, замок держится и без него

    def release(self) -> bool:
        """Отпустить замок (вложенный вход — уменьшить счётчик). → отпущен ли замок насовсем."""
        if not self._taken:
            return False
        key = self._taken.pop()
        if key is None:
            return True                   # работали без замка
        with _HELD_GUARD:
            mine = _HELD.get(key)
            if mine is None or mine["tid"] != threading.get_ident():
                self._taken.append(key)   # чужой поток замок не отпускает
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
            raise LockTimeout(self.path, describe_owner(self.path))
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _forget_locks_in_child() -> None:
    """После `fork` дочерний процесс замков не держит: унаследованные дескрипторы закрываются (без `LOCK_UN` — замок
    принадлежит открытому описанию файла, и снятие в ребёнке освободило бы его у родителя), реестр пустеет."""
    global _HELD_GUARD
    _HELD_GUARD = threading.RLock()
    for entry in list(_HELD.values()):
        try:
            os.close(entry["fd"])
        except OSError:
            pass
    _HELD.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_locks_in_child)


def describe_owner(path) -> str:
    """Кто записан владельцем замка, строкой для сообщения; пусто — записи нет или она не читается. Запись остаётся и
    после смерти владельца: перед тем как ей верить, спросите `is_locked`."""
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
    """Держит ли замок кто-то сейчас (этот или другой процесс). Не создаёт файла; замок берёт на мгновение, чтобы
    проверить, — неблокирующий `acquire` другого процесса в этот миг может получить отказ."""
    path = os.fspath(path)
    try:
        fd = _open_lock_file(path, create=False)
    except OSError:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))   # чужой файл только для чтения: flock работает
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return False
        except OSError:
            return False
    try:
        with _HELD_GUARD:
            if _file_id(fd, path) in _HELD:
                return True
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    except LockUnsupported:
        return False
    finally:
        os.close(fd)
