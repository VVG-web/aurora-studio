#!/usr/bin/env python3
"""access_check.py — кому доступна установка надстроек движка (фреймворк «Аврора»).

Корпоративный ноутбук на Windows: панель однажды запустили «от имени администратора» под
отдельной учётной записью — venv Pydantic AI лёг в профиль той записи. «Установка» была зелёной:
она смотрела глазами того же процесса. А прогон под обычной учётной записью шёл мимо Pydantic AI,
и человек не мог понять почему. Здесь то, без чего этого не различить:

* кто я — пользователь процесса, домашняя папка, повышены ли права, кто вошёл в сеанс (`whoami`);
* на чём построен venv и чей он (`venv_base_problem`, `venv_owner_problem`);
* кто что поставил на этой машине — запись в общей для всех пользователей папке после каждой
  установки (`record_install`) и чужие записи (`others`).

Сбой ОС или ctypes даёт None, а не падение: это справка, а не работа. Настоящий запуск адаптера
делает `agent_core.adapter_probe`, graphify — `aurora_extras.run_probe`; `report` собирает всё
вместе для «Установки», `kit:doctor` и `agent_core.py --venv-status`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import python_env_set, replace_file, utc_stamp  # noqa: E402

SHARED_ENV = "AURORA_SHARED_DIR"     # общая папка машины; подменяют тесты
WTS_USER_NAME, WTS_DOMAIN_NAME = 5, 7  # WTS_INFO_CLASS


# ------------------------------------------------------------------ кто я

def _win_admin(shell32=None):
    """Windows: процесс с повышенными правами (`IsUserAnAdmin`). Не Windows или сбой — None."""
    try:
        import ctypes
        return bool((shell32 or ctypes.windll.shell32).IsUserAnAdmin())
    except Exception:  # noqa: BLE001 — нет windll, нет функции: права неизвестны
        return None


def _win_session_user(wtsapi=None):
    """Windows: кто вошёл в этот сеанс (`WTSQuerySessionInformationW`) — «ДОМЕН\\имя». Сбой — None.

    Это владелец рабочего стола, а не процесса: «Запуск от имени другого пользователя» меняет второе,
    но не первое — по их расхождению и видно, что панель работает в чужом профиле."""
    try:
        import ctypes
        from ctypes import wintypes
        w = wtsapi or ctypes.WinDLL("wtsapi32")
        query, free = w.WTSQuerySessionInformationW, w.WTSFreeMemory
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                          ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        free.argtypes = [ctypes.c_void_p]

        def ask(cls: int):
            buf, size = ctypes.c_void_p(), wintypes.DWORD()
            # WTS_CURRENT_SERVER_HANDLE — NULL, WTS_CURRENT_SESSION — (DWORD)-1
            if not query(None, 0xFFFFFFFF, cls, ctypes.byref(buf), ctypes.byref(size)) or not buf.value:
                return None
            try:
                return ctypes.wstring_at(buf.value)
            finally:
                free(buf)
        name, domain = ask(WTS_USER_NAME), ask(WTS_DOMAIN_NAME)
        if not name:
            return None
        return f"{domain}\\{name}" if domain else name
    except Exception:  # noqa: BLE001 — не Windows, нет wtsapi32, служба без сеанса
        return None


def _posix_root():
    """POSIX: процесс от root (sudo). Не POSIX — None."""
    try:
        return os.geteuid() == 0
    except AttributeError:
        return None


def _posix_user():
    """POSIX: имя действующего пользователя процесса — по uid, а не по USER (sudo его меняет не всегда)."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 — нет pwd (Windows) или uid без записи
        return os.environ.get("USER") or os.environ.get("LOGNAME") or None


def _posix_login():
    try:
        return os.getlogin()
    except OSError:          # нет управляющего терминала (служба, cron)
        return None


def _bare(name) -> str:
    return str(name or "").rsplit("\\", 1)[-1].casefold()


def same_user(a, b) -> bool:
    """Один ли это пользователь: без учёта регистра; домен сравнивается, только если назван у обоих."""
    a, b = str(a or ""), str(b or "")
    if "\\" in a and "\\" in b:
        return a.casefold() == b.casefold()
    return _bare(a) == _bare(b)


def whoami(nt=None, env=None) -> dict:
    """{user, home, elevated, session_user, foreign} — глазами этого процесса.

    `elevated` — права администратора (Windows) или root (POSIX); `session_user` — кто вошёл в
    сеанс (Windows) или кто позвал sudo, иначе имя входа; `foreign` — процесс работает не от него.
    Неизвестное — None."""
    nt = os.name == "nt" if nt is None else nt
    env = os.environ if env is None else env
    if nt:
        name, domain = env.get("USERNAME") or "", env.get("USERDOMAIN") or ""
        user = (f"{domain}\\{name}" if domain else name) or None
        elevated, session = _win_admin(), _win_session_user()
    else:
        user, elevated = _posix_user(), _posix_root()
        # Процесс, который служба от root запустила со сброшенными правами (launchd, планировщик), получает
        # имя входа root — это не «в сеанс вошёл другой»: без sudo такое имя не считаем.
        login = _posix_login()
        session = env.get("SUDO_USER") or (login if login != "root" else None)
    return {"user": user, "home": str(Path.home()), "elevated": elevated, "session_user": session,
            "foreign": bool(user and session and not same_user(user, session))}


def rights_text(me: dict) -> str:
    return {True: "администратор", False: "обычные"}.get(me.get("elevated"), "не определены")


# ------------------------------------------------------------------ venv: основа и владелец

def venv_home(venv) -> str:
    """Папка базового Python из `pyvenv.cfg` (`home = …`); нет файла или строки — пусто."""
    try:
        text = (Path(venv) / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"(?mi)^\s*home\s*=\s*(.+?)\s*$", text)
    return m.group(1) if m else ""


def other_profile(path, home=None) -> bool:
    """Путь лежит в профиле ДРУГОГО пользователя: `…\\Users\\<другой>\\…`, `/Users/<другой>`, `/home/<другой>`.
    Общие профили (`Shared`, `Public`, `Default`, `All Users`) — не чужие."""
    parts = [x for x in re.split(r"[\\/]+", str(path or "")) if x]
    mine = [x.casefold() for x in re.split(r"[\\/]+", str(home if home is not None else Path.home())) if x]
    for i in (0, 1):         # от корня или от буквы диска
        if i + 1 < len(parts) and parts[i].casefold() in ("users", "home"):
            prof = [x.casefold() for x in parts[:i + 2]]
            if prof[-1] in ("shared", "public", "default", "all users"):
                return False
            return mine[:len(prof)] != prof
    return False


def venv_base_problem(venv, home=None) -> str:
    """Чем плоха основа venv для этого пользователя: `other_user` — Python в чужом профиле, `denied` —
    недоступен, `missing` — его нет; пусто — годится (или venv нет вовсе: это другая причина)."""
    base = venv_home(venv)
    if not base:
        return ""
    if other_profile(base, home):
        return "other_user"
    try:
        os.stat(base)
    except PermissionError:
        return "denied"
    except OSError:
        return "missing"
    return "" if os.access(base, os.R_OK | os.X_OK) else "denied"


def venv_owner_problem(venv) -> str:
    """POSIX: venv другого владельца (`other_uid` — например, ставили через sudo) или закрыт на запись
    (`readonly`): работает, но обновить его без прав владельца нельзя. Windows и нет venv — пусто."""
    if os.name == "nt":
        return ""
    try:
        st = os.stat(venv)
    except OSError:
        return ""
    if st.st_uid != os.geteuid():
        return "other_uid"
    return "" if os.access(venv, os.W_OK) else "readonly"


# ------------------------------------------------------------------ кто что поставил

def shared_dir(nt=None, platform=None) -> Path:
    """Общая для всех пользователей машины папка Авроры: `%ProgramData%\\AuroraStudio`,
    `/Users/Shared/AuroraStudio` (macOS), `/var/tmp/aurora-studio` (Linux). `AURORA_SHARED_DIR` — своя."""
    own = os.environ.get(SHARED_ENV)
    if own:
        return Path(own)
    if os.name == "nt" if nt is None else nt:
        return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "AuroraStudio"
    if (platform or sys.platform) == "darwin":
        return Path("/Users/Shared/AuroraStudio")
    return Path("/var/tmp/aurora-studio")


def installs_dir() -> Path:
    return shared_dir() / "installs"


def _file_name(user) -> str:
    return re.sub(r"[^\w.-]+", "_", str(user or "unknown")).strip("._") or "unknown"


def _shared_mkdir(folder: Path) -> None:
    """Папки создаёт первый пользователь, а писать в них будут все: на POSIX — 1777, как у /tmp
    (свой файл каждый может заменить, чужой — нет). На Windows так уже наследуется от ProgramData."""
    for d in (folder.parent, folder):
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                try:
                    os.chmod(d, 0o1777)
                except OSError:
                    pass


def record_install(component: str, version: str, path, me=None) -> str:
    """Записать «я поставил» в общую папку машины: файл свой у каждого пользователя, запись атомарная.
    → пусто или причина (одной строкой для журнала установки: запись — сведение, установку она не роняет)."""
    me = me or whoami()
    user = me.get("user") or "unknown"
    folder = installs_dir()
    target = folder / f"{_file_name(user)}.json"
    try:
        _shared_mkdir(folder)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        rows = data.get("components") if isinstance(data, dict) and isinstance(data.get("components"), dict) else {}
        rows[component] = {"component": component, "version": str(version or ""), "path": str(path or ""),
                           "user": user, "elevated": me.get("elevated"), "at": utc_stamp()}
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"user": user, "components": rows}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o644)
        replace_file(tmp, target)
    except OSError as e:
        return f"запись об установке не сохранена в общей папке машины ({folder}): {e.strerror or type(e).__name__}"
    return ""


def others(component: str, me=None) -> list:
    """Записи о `component` ДРУГИХ пользователей машины, свежие первыми. Битый файл пропускается."""
    me = me or whoami()
    out = []
    try:
        files = sorted(installs_dir().glob("*.json"))
    except OSError:
        return []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            row = (data.get("components") or {}).get(component)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(row, dict) and row.get("user") and not same_user(row["user"], me.get("user")):
            out.append({k: row.get(k) for k in ("component", "version", "path", "user", "elevated", "at")})
    return sorted(out, key=lambda r: str(r.get("at") or ""), reverse=True)


def others_date(component: str) -> str:
    """Дата самой свежей чужой записи (`ГГГГ-ММ-ДД`) — для итога прогона, где имён нет; пусто — записей нет."""
    try:
        rows = others(component)
    except Exception:  # noqa: BLE001 — сведение не роняет прогон
        return ""
    return str(rows[0].get("at") or "")[:10] if rows else ""


def other_line(row: dict, title: str, me: dict) -> str:
    """Строка для терминала и доктора: у кого стоит и почему вам не видно."""
    when = str(row.get("at") or "")[:10] or "дата не записана"
    admin = ", с правами администратора" if row.get("elevated") else ""
    return (f"{title} {row.get('version') or '?'} стоит у пользователя {row.get('user')} (поставлен {when}{admin}) "
            f"в {row.get('path') or '?'} — для вас ({me.get('user') or '?'}) он недоступен: "
            "каждый пользователь ставит его себе")


# ------------------------------------------------------------------ всё вместе

def report(adapter=None, cwd=None) -> dict:
    """Кто я, что из окружения снимается для venv и как работают надстройки ДЛЯ МЕНЯ — настоящим запуском,
    как в прогоне (`adapter` — файл адаптера той копии движка, которой пойдёт прогон; `cwd` — папка проекта)."""
    import agent_core as AG
    import aurora_extras as EX
    me = whoami()
    comps = {}
    for eid, ex in EX.EXTRAS.items():
        run = AG.adapter_probe(adapter, cwd) if eid == "pydantic-ai" else EX.run_probe(eid)
        owner = venv_owner_problem(ex["venv"])
        comps[eid] = {"title": ex["title"], "venv": str(ex["venv"]), "run": run, "owner": owner,
                      # `note` — ключ сообщения: панель переводит его по таблице причин (`CAUSES`)
                      "note": str(AG.Why("venv_not_yours", path=AG._shown_path(ex["venv"]))) if owner else "",
                      # не моё и не запускается — тогда и важно, у кого оно есть
                      "others": [] if run.get("ok") else others(eid, me)}
    return {"me": me, "python_env": python_env_set(), "components": comps, "at": utc_stamp()}


def report_lines(rep: dict) -> list:
    """`report` для терминала: пользователь и права, снимаемые переменные, настоящий запуск, чужие записи."""
    me = rep["me"]
    lines = [f"Проверено для: {me.get('user') or '?'} · домашняя папка {me.get('home')} · права: {rights_text(me)}"]
    if me.get("foreign") or me.get("elevated"):
        who = me.get("user") or "?"
        lines.append(f"⚠️ Процесс работает от имени {who}" + (" с правами администратора" if me.get("elevated") else "")
                     + (f", а в сеанс вошёл {me['session_user']}" if me.get("foreign") else "")
                     + f": всё, что ставится отсюда, ляжет в профиль {who} и не будет видно, "
                     "когда вы запустите Аврору обычным способом")
    env = rep.get("python_env") or []
    if env:
        lines.append(f"В окружении задана {', '.join(env)} — для venv движок её снимает" if len(env) == 1 else
                     f"В окружении заданы {', '.join(env)} — для venv движок их снимает")
    for c in rep["components"].values():
        run = c["run"]
        lines.append(f"{c['title']}: " + (f"работает для вас (настоящий запуск, {run.get('version') or 'версия не названа'})"
                                          if run.get("ok") else f"для вас не запускается — {run.get('why')}"))
        if c.get("note"):
            lines.append(f"  ⚠️ {c['note']}")
        for row in c.get("others") or []:
            lines.append("  " + other_line(row, c["title"], me))
    return lines
