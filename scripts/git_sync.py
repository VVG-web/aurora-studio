#!/usr/bin/env python3
"""git_sync.py — Git проекта: настройка, состояние, обновление и отправка (фреймворк «Аврора»).

У каждого проекта свой сервер и свой вход. Настройка лежит в `.git/aurora/git.json`
проекта: она не уходит в историю и живёт вместе с клоном. Секреты лежат отдельно и вне
проекта — в `~/.aurora/git/credentials.json` (права 600), наружу отдаётся только маска.
Токен не пишется ни в адрес удалённого репозитория, ни в командную строку: git получает
его из переменной окружения через свой механизм учётных данных.

  python3 git_sync.py --status [--fetch] [--json]
  python3 git_sync.py --update [--strategy ff-only|merge|rebase] [--commit-first]
  python3 git_sync.py --push [--message ТЕКСТ] [--no-commit] [--update-first]
  python3 git_sync.py --commit [--message ТЕКСТ] [--skip-ratchet]
  python3 git_sync.py --fix --what abort|continue|mine|theirs|resolved|set-upstream|init|checkout|use-branch|strip-url|fix-key-perms [--file ПУТЬ]
  python3 git_sync.py --check          # вход, репозиторий и ветка на сервере
  python3 git_sync.py --modules        # модули провайдеров: что стоит, что в ките

Неудача — всегда с причиной человеческими словами, советом и действием, которым это
чинится: панель получает код (`problem.code`) и действия (`problem.actions`), командная
строка — те же слова. Сырой вывод git идёт ниже, для того, кто хочет разобраться сам.

Автоматика (`--auto СОБЫТИЕ`) делает работу, только если в настройке проекта она включена
на это событие: устаревший тик или чужой вызов ничего не обновит и не отправит.

Провайдеры — модули: Gitea, GitLab, Bitbucket (`gitproviders/<id>/` в ките, ставятся в
`~/.aurora/git-providers/` со страницы «Установка»). Без модуля работает любой git-сервер:
модуль добавляет проверку входа и прав через API, ветку по умолчанию и адреса клонирования.

Панель: раздел «Git» — `git:status`, `git:update`, `git:push`, `git:commit`, `git:fix`, `git:check`
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
for _s in (sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на «—» (UnicodeEncodeError);
    # движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MASK = "••••••"
PROVIDERS = ("gitea", "gitlab", "bitbucket", "generic")
AUTHS = ("system", "token", "password", "ssh")
STRATEGIES = ("ff-only", "merge", "rebase")
UPDATE_ON = ("open", "interval", "before_route")
PUSH_ON = ("after_route", "after_commit", "interval")
PLACEHOLDERS = ("project", "date", "time", "branch", "count", "files", "trigger")
FIXES = ("abort", "continue", "mine", "theirs", "resolved", "set-upstream", "init",
         "checkout", "use-branch", "strip-url", "fix-key-perms")
MIN_EVERY = 5                 # минут: чаще ходить на сервер незачем
DEBOUNCE_S = 120              # «после фиксации» — когда фиксации перестали сыпаться
OPEN_EVERY_S = 600            # «при открытии» — не чаще раза в десять минут
FAIL_BACKOFF_S = 900          # упавшую автоматику не повторяем каждую минуту
LOG_KEEP = 300
LOCK_STALE_S = 1800
NET_TIMEOUT = 180
LIST_KEEP = 200

TRIGGER_WORDS = {"manual": "вручную", "open": "при открытии проекта",
                 "interval": "по расписанию", "before_route": "перед маршрутом",
                 "after_route": "после маршрута", "after_commit": "после фиксации"}

DEFAULTS = {
    "version": 1,
    "provider": "generic",
    "instance": "",
    "repo": "",
    "remote": "origin",
    "branch": "",
    "author_name": "",
    "author_email": "",
    "message": "Aurora: {project} — {date} {time}",
    "strategy": "ff-only",
    "commit_on_push": True,
    "tls": {"verify": True, "ca_file": ""},
    "auto_update": {"enabled": False, "on": ["open"], "every_min": 60},
    "auto_push": {"enabled": False, "on": ["after_route"], "every_min": 60},
}
CRED_DEFAULT = {"auth": "system", "user": "", "secret": "", "ssh_key": ""}

# Код → (что случилось, что сделать, действия панели). Тексты — для командной строки и
# журнала; панель показывает те же смыслы своими каталогами строк по коду.
PROBLEMS = {
    "no_git": ("git не установлен на этой машине",
               "Поставьте git — страница «Установка» скажет как.", ["open_install"]),
    "no_repo": ("Проект ещё не под git",
                "Создайте репозиторий: история изменений начнётся с этой минуты.", ["init"]),
    "no_remote": ("Сервер для проекта не задан",
                  "Укажите в настройке Git адрес репозитория на сервере.", ["open_setup"]),
    "bad_url": ("Адрес репозитория записан неверно",
                "Проверьте адрес в настройке Git: https://сервер/владелец/репозиторий "
                "или git@сервер:владелец/репозиторий.", ["open_setup"]),
    "detached": ("Проект не на ветке",
                 "Перейдите на ветку проекта — иначе новые фиксации некуда отправить.",
                 ["checkout_branch"]),
    "other_branch": ("Открыта не та ветка",
                     "Перейдите на ветку из настройки или сделайте открытую веткой проекта.",
                     ["checkout_branch", "use_current_branch"]),
    "auth": ("Сервер не принял вход",
             "Проверьте логин и токен (или пароль) в настройке Git: токен мог истечь или "
             "не иметь прав на репозиторий.", ["open_credentials", "check"]),
    "forbidden": ("Не хватает прав",
                  "Вход принят, но прав на это действие нет: попросите у владельца "
                  "репозитория право записи или выберите другую ветку.",
                  ["open_setup", "check"]),
    "ssh_key": ("Сервер не принял SSH-ключ",
                "Добавьте публичную часть ключа в профиль на сервере или укажите другой "
                "ключ.", ["open_credentials"]),
    "ssh_hostkey": ("Сервер представился незнакомым SSH-ключом",
                    "Если сервер переустанавливали — уберите старую запись о нём из "
                    "~/.ssh/known_hosts; если нет — сверьте отпечаток с администратором: "
                    "это может быть подмена.", ["open_setup"]),
    "ssh_passphrase": ("SSH-ключ защищён паролем",
                       "Добавьте ключ в ssh-agent (ssh-add) или укажите ключ без пароля.",
                       ["open_credentials"]),
    "ssh_key_perms": ("SSH-ключ открыт другим пользователям",
                      "ssh не работает с таким ключом: закройте его правами 600.",
                      ["fix_key_perms"]),
    "cert": ("Сертификат сервера не прошёл проверку",
             "Если сервер ваш и у него собственный сертификат — доверьтесь ему по "
             "отпечатку; иначе проверьте адрес сервера.", ["trust_cert", "open_setup"]),
    "network": ("Сервер недоступен",
                "Проверьте сеть, VPN и адрес сервера; повторите позже.",
                ["retry", "open_setup"]),
    "repo_not_found": ("Репозиторий на сервере не найден",
                       "Проверьте владельца и имя репозитория. У закрытого репозитория это "
                       "же значит «нет прав на чтение».", ["open_setup", "check"]),
    "remote_branch_missing": ("На сервере нет этой ветки",
                              "Отправьте проект — ветка появится на сервере.", ["push"]),
    "no_upstream": ("Ветка не связана с сервером",
                    "Свяжите её: обновление и отправка будут знать, куда идти.",
                    ["set_upstream"]),
    "rejected": ("На сервере есть изменения, которых у вас нет",
                 "Сначала обновите проект, потом отправляйте.", ["update_then_push"]),
    "protected": ("Сервер не принимает отправку в эту ветку",
                  "Ветка защищена правилами сервера: отправьте в другую ветку или "
                  "попросите права у владельца.", ["open_setup"]),
    "diverged": ("Ваши и серверные изменения разошлись",
                 "Безопасное обновление (только перемотка) здесь невозможно: объедините "
                 "изменения слиянием или перенесите свои поверх серверных.",
                 ["update_merge", "update_rebase"]),
    "conflict": ("Изменения пересеклись в одних и тех же местах",
                 "Выберите для каждого файла, чья версия верна, или поправьте его вручную, "
                 "затем завершите обновление. Его можно и отменить.",
                 ["show_conflicts", "abort"]),
    "conflict_markers": ("В файле остались метки конфликта",
                         "Уберите строки <<<<<<<, ======= и >>>>>>> — или выберите версию "
                         "целиком.", ["show_conflicts"]),
    "in_progress": ("Прошлое обновление не закончено",
                    "Завершите его или отмените.", ["show_conflicts", "abort"]),
    "dirty": ("Незафиксированные изменения мешают обновлению",
              "Зафиксируйте их — обновление пройдёт поверх.", ["commit_then_update"]),
    "locked": ("С git проекта сейчас работает другая программа",
               "Подождите и повторите; если никто не работает — закройте редактор или "
               "терминал с git.", ["retry"]),
    "busy": ("Git-действие в проекте уже идёт", "Дождитесь его конца.", ["retry"]),
    "ratchet": ("Фиксацию остановил храповик: в изменениях новые ошибки базы",
                "Почините ошибки (kb:lint) или зафиксируйте всё равно — решение ваше.",
                ["commit_anyway"]),
    "msg_hook": ("В сообщении фиксации — внутреннее название",
                 "Перепишите шаблон сообщения: внутренние названия в историю не уходят.",
                 ["open_setup"]),
    "unrelated": ("История на сервере не связана с историей проекта",
                  "Похоже, указан чужой репозиторий. Проверьте адрес; новый репозиторий "
                  "создавайте пустым, без README.", ["open_setup"]),
    "creds_in_url": ("В адресе сервера записан пароль",
                     "Он лежит открытым текстом в .git/config. Перенесите его в защищённое "
                     "хранилище.", ["strip_url"]),
    "no_commits": ("В проекте ещё нет ни одной фиксации",
                   "Зафиксируйте изменения — тогда будет что отправить.", ["commit"]),
    "module_missing": ("Модуль провайдера не установлен",
                       "Обновление и отправка работают и без него; проверка входа и прав "
                       "через API появится после установки.", ["install_module"]),
    "module_outdated": ("Модуль провайдера устарел",
                        "В ките есть версия новее — обновите модуль.", ["update_module"]),
    "settings": ("Настройка Git заполнена не полностью",
                 "Откройте настройку: неверные поля подсвечены.", ["open_setup"]),
    "unknown": ("Git сообщил об ошибке", "Подробности — ниже, в выводе git.", ["retry"]),
}

# Разбор вывода git. Порядок важен: «защищённая ветка» говорит и «failed to push», а
# SSH-отказы заканчиваются общим «Could not read from remote repository». Git
# запускается с LC_ALL=C — иначе на русской Windows те же отказы приходят переведёнными.
PATTERNS = [
    ("no_git", r"git: command not found|No such file or directory: 'git'"),
    ("cert", r"SSL certificate problem|self[- ]signed certificate|unable to get local issuer|"
             r"certificate verify failed|server certificate verification failed|"
             r"certificate has expired|SSL_ERROR|schannel"),
    ("ssh_hostkey", r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED"),
    ("ssh_key_perms", r"UNPROTECTED PRIVATE KEY FILE|bad permissions"),
    ("ssh_passphrase", r"passphrase"),
    ("ssh_key", r"Permission denied \(publickey|no such identity|Load key .*invalid format"),
    ("auth", r"Authentication failed|HTTP Basic: Access denied|Invalid username or password|"
             r"could not read (Username|Password)|terminal prompts disabled|"
             r"returned error: 401|Unauthorized|invalid credentials"),
    ("protected", r"protected branch|pre-receive hook declined|GL-HOOK-ERR|"
                  r"not allowed to (force )?push|branch is protected|\[remote rejected\]"),
    ("forbidden", r"returned error: 403|Permission to .* denied|You are not allowed|"
                  r"403 Forbidden|insufficient permission|access denied|not authorized"),
    ("remote_branch_missing", r"couldn't find remote ref|remote ref does not exist"),
    ("repo_not_found", r"repository .*not found|Repository not found|"
                       r"does not appear to be a git repository|returned error: 404|"
                       r"Could not read from remote repository"),
    ("network", r"Could not resolve host|Failed to connect|Connection refused|"
                r"Connection timed out|Operation timed out|Network is unreachable|"
                r"Couldn't connect to server|Connection reset|timeout after|No route to host|"
                r"Name or service not known|nodename nor servname|Could not resolve hostname"),
    ("bad_url", r"URL using bad/illegal format|Unsupported protocol|protocol '.*' is not "
                r"supported|is not a valid|malformed"),
    ("no_upstream", r"has no upstream branch|no tracking information"),
    ("rejected", r"\[rejected\]|non-fast-forward|Updates were rejected|fetch first"),
    ("diverged", r"Not possible to fast-forward|diverging branches|cannot fast-forward"),
    ("conflict", r"CONFLICT|Automatic merge failed|could not apply|Resolve all conflicts|"
                 r"needs merge|You have unmerged paths|Applying autostash resulted in conflicts"),
    ("dirty", r"would be overwritten by (merge|checkout)|Please commit your changes or stash|"
              r"You have unstaged changes|cannot (pull|rebase) with"),
    ("locked", r"index\.lock'?: File exists|Another git process seems to be running|"
               r"Unable to create .*\.lock"),
    ("unrelated", r"refusing to merge unrelated histories"),
]
_RX = [(code, re.compile(rx, re.I)) for code, rx in PATTERNS]
_SECRETS: set = set()          # что вычищать из любого вывода


# ---------------------------------------------------------------- мелочи

def utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def aurora_home() -> Path:
    return Path.home() / ".aurora"


def _vt(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def redact(text: str) -> str:
    """Вывод без секретов: пароль в адресе и известные токены — под маску."""
    if not text:
        return text
    text = re.sub(r"(\w+://)[^/\s@]+@", r"\1***@", text)
    for secret in _SECRETS:
        if secret and len(secret) >= 4:
            text = text.replace(secret, MASK)
    return text


def strip_userinfo(url: str) -> str:
    """https://логин:пароль@сервер/… → https://сервер/… (ssh-логин git@ остаётся)."""
    m = re.match(r"^(https?://)([^/@\s]+)@(.*)$", url or "", re.I)
    return m.group(1) + m.group(3) if m else (url or "")


def url_userinfo(url: str) -> tuple:
    """(логин, пароль) из https-адреса; чего нет — пусто."""
    m = re.match(r"^https?://([^/@\s]+)@", url or "", re.I)
    if not m:
        return "", ""
    user, _, pw = m.group(1).partition(":")
    return urllib.parse.unquote(user), urllib.parse.unquote(pw)


URL_RX = re.compile(r"^https?://[^\s/@]+(?::\d{1,5})?(?:/[^\s]*)?$", re.I)
SCP_RX = re.compile(r"^[\w.-]+@[\w.-]+:[^\s]+$")
SSH_URL_RX = re.compile(r"^ssh://[^\s]+$", re.I)
PATH_RX = re.compile(r"^~?[\w.-]+(?:/[\w.-]+)+$")
# Сетевая папка или локальный путь — тоже сервер для git: `file://…`, `/srv/repo.git`,
# `C:\repos\x.git`, `\\сервер\папка`.
LOCAL_RX = re.compile(r"^(file://\S+|/\S+|[A-Za-z]:[\\/]\S*|\\\\\S+)$")
REMOTE_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_url(repo: str) -> bool:
    return bool(URL_RX.match(repo or "") or SCP_RX.match(repo or "") or SSH_URL_RX.match(repo or "")
                or LOCAL_RX.match(repo or ""))


def valid_branch(name: str) -> bool:
    """Правила `git check-ref-format --branch` без запуска git."""
    if not name or name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if any(x in name for x in ("..", "@{", "//", "\\")) or name == "@":
        return False
    return not re.search(r"[\x00-\x20\x7f~^:?*\[]", name)


def _host(url: str) -> str:
    if SCP_RX.match(url or ""):
        return url.split("@", 1)[1].split(":", 1)[0]
    try:
        return urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""


def guess_provider(url: str) -> str:
    host = _host(url).lower()
    if "gitlab" in host:
        return "gitlab"
    if "bitbucket" in host:
        return "bitbucket"
    if "gitea" in host or "forgejo" in host or "codeberg" in host:
        return "gitea"
    return "generic"


def problem(code: str, raw: str = "", **extra) -> dict:
    title, fix, actions = PROBLEMS.get(code, PROBLEMS["unknown"])
    return {"code": code, "title": title, "fix": fix, "actions": list(actions),
            "raw": redact(raw or "")[-2500:], **extra}


def classify(text: str) -> str:
    for code, rx in _RX:
        if rx.search(text or ""):
            return code
    return "unknown"


def result(action: str, ok: bool, summary: str = "", prob: dict | None = None, **extra) -> dict:
    return {"action": action, "ok": ok, "summary": summary, "problem": prob, **extra}


# ---------------------------------------------------------------- кит и модули

def kit_root() -> Path | None:
    """Кит: сам скрипт в `scripts/` кита либо копия движка проекта с указателем на кит."""
    here = Path(__file__).resolve().parent
    root = here.parent
    if root.name == ".opencode":
        ptr = root / "kit_path.txt"
        if ptr.is_file():
            kit = Path(ptr.read_text(encoding="utf-8").strip())
            return kit if kit.is_dir() else None
        return None
    return root


def bundled_dir(kit: Path | None = None) -> Path | None:
    kit = kit or kit_root()
    d = kit / "gitproviders" if kit else None
    return d if d and d.is_dir() else None


def installed_dir() -> Path:
    return aurora_home() / "git-providers"


def read_manifest(folder: Path) -> dict:
    data = _read_json(folder / "provider.json", {})
    return data if data.get("id") else {}


def module_rows(kit: Path | None = None, github: dict | None = None) -> list:
    """Строка на модуль: что стоит на машине, что в ките, что вышло на GitHub."""
    bundle = bundled_dir(kit)
    ids = set()
    if bundle:
        ids |= {p.name for p in bundle.iterdir() if (p / "provider.json").is_file()}
    if installed_dir().is_dir():
        ids |= {p.name for p in installed_dir().iterdir() if (p / "provider.json").is_file()}
    rows = []
    for mid in sorted(ids):
        have = read_manifest(installed_dir() / mid)
        kitm = read_manifest(bundle / mid) if bundle else {}
        meta = kitm or have
        gh = (github or {}).get(mid, "")
        rows.append({
            "id": mid, "title": meta.get("title", mid), "about": meta.get("about", {}),
            "installed": have.get("version", ""), "kit": kitm.get("version", ""),
            "github": gh,
            "missing": not have,
            "update": bool(have and kitm and _vt(kitm["version"]) > _vt(have.get("version", ""))),
            "github_newer": bool(gh and kitm and _vt(gh) > _vt(kitm.get("version", ""))),
            "path": str(installed_dir() / mid),
        })
    return rows


def module_state(provider: str, kit: Path | None = None) -> dict:
    """Нужен ли проекту модуль и в каком он состоянии: builtin | missing | outdated | ok."""
    if provider == "generic":
        return {"id": "generic", "state": "builtin"}
    have = read_manifest(installed_dir() / provider)
    bundle = bundled_dir(kit)
    kitm = read_manifest(bundle / provider) if bundle else {}
    if not have:
        return {"id": provider, "state": "missing", "available": kitm.get("version", ""),
                "title": kitm.get("title", provider)}
    if kitm and _vt(kitm.get("version", "")) > _vt(have.get("version", "")):
        return {"id": provider, "state": "outdated", "installed": have.get("version", ""),
                "available": kitm["version"], "title": kitm.get("title", provider)}
    return {"id": provider, "state": "ok", "installed": have.get("version", ""),
            "title": have.get("title", provider)}


_ADAPTERS: dict = {}


def load_adapter(mid: str):
    """Адаптер провайдера из установленного модуля. Нет или не грузится — None."""
    if mid not in PROVIDERS or mid == "generic":
        return None
    path = installed_dir() / mid / "adapter.py"
    if not path.is_file():
        return None
    key = (str(path), path.stat().st_mtime)
    if _ADAPTERS.get(mid, (None, None))[0] == key:
        return _ADAPTERS[mid][1]
    try:
        spec = importlib.util.spec_from_file_location(f"aurora_gitprovider_{mid}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:                                        # noqa: BLE001
        return None
    for need in ("check", "clone_urls"):
        if not callable(getattr(mod, need, None)):
            return None
    _ADAPTERS[mid] = (key, mod)
    return mod


def install_module(mid: str, kit: Path | None = None) -> dict:
    """Поставить или обновить модуль из кита. Новый сначала проверяется загрузкой: не
    грузится — прежний остаётся на месте."""
    bundle = bundled_dir(kit)
    src = bundle / mid if bundle else None
    if not src or not read_manifest(src):
        return {"ok": False, "error": f"в ките нет модуля «{mid}»"}
    dst = installed_dir() / mid
    tmp = installed_dir() / f".{mid}.new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    try:
        spec = importlib.util.spec_from_file_location(f"aurora_gitprovider_check_{mid}",
                                                      tmp / "adapter.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not (callable(getattr(mod, "check", None)) and callable(getattr(mod, "clone_urls", None))):
            raise ImportError("в адаптере нет check() или clone_urls()")
    except Exception as e:                                   # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "error": f"модуль «{mid}» из кита не загрузился: {e}"}
    old = installed_dir() / f".{mid}.old"
    shutil.rmtree(old, ignore_errors=True)
    if dst.exists():
        os.replace(dst, old)
    os.replace(tmp, dst)
    shutil.rmtree(old, ignore_errors=True)
    _ADAPTERS.pop(mid, None)
    return {"ok": True, "id": mid, "version": read_manifest(dst).get("version", "")}


GH_CACHE_TTL = 6 * 3600
GH_FAIL_TTL = 1800             # без сети — не спрашиваем на каждом открытии «Установки»


def github_versions(slug: str, branch: str = "master", fresh: bool = False,
                    ids: list | None = None) -> dict:
    """{модуль: версия} из последнего состояния кита на GitHub. Кэш шесть часов, неудача —
    полчаса. Первая сетевая ошибка останавливает опрос: ждать таймаут на каждом модуле —
    это минута висящей страницы без сети. Модуля на GitHub нет (404) — просто пропуск."""
    cache_path = aurora_home() / "gitmods-cache.json"
    cache = _read_json(cache_path, {})
    ttl = GH_FAIL_TTL if cache.get("error") else GH_CACHE_TTL
    if not fresh and cache.get("slug") == slug and time.time() - cache.get("at", 0) < ttl:
        return cache.get("versions", {})
    out, err = {}, ""
    for mid in ids or [p for p in PROVIDERS if p != "generic"]:
        url = f"https://raw.githubusercontent.com/{slug}/{branch}/gitproviders/{mid}/provider.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aurora-studio"})
            with urllib.request.urlopen(req, timeout=10) as r:
                out[mid] = json.loads(r.read(100_000).decode("utf-8", "replace")).get("version", "")
        except urllib.error.HTTPError:
            continue
        except Exception as e:                               # noqa: BLE001
            err = str(e)[:160] or type(e).__name__
            break
    try:
        _write_json(cache_path, {"slug": slug, "at": time.time(), "versions": out, "error": err})
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- запуск git

HELPER = ('!f() { test "$1" = get || exit 0; '
          'printf "%s\\n" "username=$AURORA_GIT_USER" "password=$AURORA_GIT_PASS"; }; f')


def _env(localized: bool = False) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "Malloc"))}
    for k in ("GIT_ASKPASS", "SSH_ASKPASS", "AURORA_GIT_USER", "AURORA_GIT_PASS"):
        env.pop(k, None)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_EDITOR="true",
               GIT_MERGE_AUTOEDIT="no", SSH_ASKPASS_REQUIRE="never", PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8")
    if not localized:
        # Отказы git разбираются по английскому тексту: переведённый на русской Windows
        # вывод иначе классифицировался бы как «неизвестная ошибка».
        env.update(LC_ALL="C", LANG="C", LANGUAGE="C")
    return env


def _quote(path: str) -> str:
    return shlex.quote(path.replace("\\", "/"))


def auth_config(s: dict, c: dict, env: dict) -> list:
    """Флаги `-c` и переменные окружения для входа: токен — через помощника учётных
    данных из переменных, ключ — через GIT_SSH_COMMAND. Ни секрет, ни путь к нему не
    попадают в аргументы командной строки, которые видит `ps`."""
    cfg = []
    tls = s.get("tls") or {}
    if tls.get("verify") is False:
        cfg += ["-c", "http.sslVerify=false"]
    elif tls.get("ca_file"):
        cfg += ["-c", f"http.sslCAInfo={tls['ca_file']}"]
    auth = c.get("auth") or "system"
    if auth in ("token", "password") and c.get("secret"):
        env["AURORA_GIT_USER"] = c.get("user") or default_user(s)
        env["AURORA_GIT_PASS"] = c["secret"]
        _SECRETS.add(c["secret"])
        cfg += ["-c", "credential.helper=", "-c", "credential.helper=" + HELPER]
    elif auth == "ssh" and c.get("ssh_key"):
        env["GIT_SSH_COMMAND"] = ("ssh -i " + _quote(c["ssh_key"]) + " -o IdentitiesOnly=yes "
                                  "-o BatchMode=yes -o StrictHostKeyChecking=accept-new")
    return cfg


def default_user(s: dict) -> str:
    """Логин к токену, когда человек его не задал: GitLab и Bitbucket Cloud принимают
    условный, остальным нужен настоящий (его подставляет «Проверить подключение»)."""
    if s.get("provider") == "gitlab":
        return "oauth2"
    if s.get("provider") == "bitbucket" and "bitbucket.org" in (s.get("instance") or ""):
        return "x-token-auth"
    return "aurora"


def run_git(project, args: list, s: dict | None = None, c: dict | None = None,
            timeout: int = 60, network: bool = False, localized: bool = False,
            extra_env: dict | None = None) -> tuple:
    """(код, stdout, stderr) без секретов в выводе. Ни один вызов не идёт через оболочку."""
    env = _env(localized)
    cfg = ["-c", "core.quotepath=false"]
    if network and s is not None and c is not None:
        cfg += auth_config(s, c, env)
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run(["git", *cfg, "-C", str(project), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env=env, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "", "git: command not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    return r.returncode, redact((r.stdout or "").strip()), redact((r.stderr or "").strip())


def git_ok(project, *args) -> str:
    rc, out, _ = run_git(project, list(args))
    return out if rc == 0 else ""


def git_dir(project) -> Path | None:
    rc, out, _ = run_git(project, ["rev-parse", "--absolute-git-dir"])
    return Path(out) if rc == 0 and out else None


def has_git() -> bool:
    return bool(shutil.which("git"))


# ---------------------------------------------------------------- настройка

def store_dir(project) -> Path | None:
    gd = git_dir(project)
    return gd / "aurora" if gd else None


def settings_file(project) -> Path | None:
    d = store_dir(project)
    return d / "git.json" if d else None


def project_name(project) -> str:
    cfg = Path(project) / "aurora.config.yaml"
    try:
        m = re.search(r"^\s*name:\s*[\"']?([^\"'\n#]+)", cfg.read_text(encoding="utf-8"), re.M)
        if m and m.group(1).strip():
            return m.group(1).strip()
    except OSError:
        pass
    return Path(project).resolve().name


def _bool(v, d: bool) -> bool:
    return v if isinstance(v, bool) else d


def _int(v, d: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def normalize(data) -> tuple:
    """(настройка, замечания по полям). Замечание — {field, code}: панель подсвечивает
    поле и пишет причину своим каталогом строк."""
    d = data if isinstance(data, dict) else {}
    s = json.loads(json.dumps(DEFAULTS))
    probs = []

    def bad(field, code):
        probs.append({"field": field, "code": code})

    prov = str(d.get("provider") or "generic").strip().lower()
    if prov not in PROVIDERS:
        bad("provider", "unknown")
        prov = "generic"
    s["provider"] = prov
    inst = str(d.get("instance") or "").strip().rstrip("/")
    if inst and not URL_RX.match(inst):
        bad("instance", "url")
    s["instance"] = strip_userinfo(inst)
    repo = str(d.get("repo") or "").strip()
    if repo != strip_userinfo(repo):
        bad("repo", "creds_in_url")
        repo = strip_userinfo(repo)
    if repo and not is_url(repo):
        repo = repo.strip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not PATH_RX.match(repo):
            bad("repo", "repo")
        elif not inst:
            bad("instance", "required")
    s["repo"] = repo
    remote = str(d.get("remote") or "origin").strip()
    if not REMOTE_RX.match(remote):
        bad("remote", "name")
    s["remote"] = remote
    branch = str(d.get("branch") or "").strip()
    if branch and not valid_branch(branch):
        bad("branch", "branch")
    s["branch"] = branch
    s["author_name"] = " ".join(str(d.get("author_name") or "").split())
    email = str(d.get("author_email") or "").strip()
    if email and not EMAIL_RX.match(email):
        bad("author_email", "email")
    s["author_email"] = email
    msg = str(d.get("message") or "").strip() or DEFAULTS["message"]
    unknown = [p for p in re.findall(r"\{(\w+)\}", msg) if p not in PLACEHOLDERS]
    if unknown:
        bad("message", "placeholder")
    s["message"] = msg
    strat = str(d.get("strategy") or "ff-only")
    s["strategy"] = strat if strat in STRATEGIES else "ff-only"
    s["commit_on_push"] = _bool(d.get("commit_on_push"), True)
    tls = d.get("tls") if isinstance(d.get("tls"), dict) else {}
    ca = str(tls.get("ca_file") or "").strip()
    if ca and not Path(os.path.expanduser(ca)).is_file():
        bad("ca_file", "missing")
    s["tls"] = {"verify": _bool(tls.get("verify"), True), "ca_file": os.path.expanduser(ca) if ca else ""}
    for key, allowed in (("auto_update", UPDATE_ON), ("auto_push", PUSH_ON)):
        src = d.get(key) if isinstance(d.get(key), dict) else {}
        on = [x for x in (src.get("on") if isinstance(src.get("on"), list) else DEFAULTS[key]["on"])
              if x in allowed]
        every = _int(src.get("every_min"), DEFAULTS[key]["every_min"])
        enabled = _bool(src.get("enabled"), False)
        if enabled and not on:
            bad(key, "no_trigger")
        if "interval" in on and every < MIN_EVERY:
            bad(key, "every")
            every = MIN_EVERY
        s[key] = {"enabled": enabled, "on": on, "every_min": every}
    return s, probs


def derived(project) -> dict:
    """Настройка по тому, что уже есть в репозитории: удалённый сервер, ветка, автор."""
    s = json.loads(json.dumps(DEFAULTS))
    remotes = git_ok(project, "remote").split()
    remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "origin")
    url = git_ok(project, "remote", "get-url", remote) if remotes else ""
    clean = strip_userinfo(url)
    s.update(remote=remote, repo=clean, provider=guess_provider(clean),
             branch=git_ok(project, "branch", "--show-current"),
             author_name=git_ok(project, "config", "user.name"),
             author_email=git_ok(project, "config", "user.email"))
    if clean and URL_RX.match(clean):
        p = urllib.parse.urlparse(clean)
        s["instance"] = f"{p.scheme}://{p.netloc}"
    return s


def load(project) -> dict:
    """Сохранённая настройка; её нет — выведенная из репозитория (не сохраняется)."""
    f = settings_file(project)
    if f and f.is_file():
        return normalize(_read_json(f, {}))[0]
    return derived(project)


def load_saved(project) -> dict | None:
    """Только сохранённая: автоматика без явной настройки человека не работает."""
    f = settings_file(project)
    return normalize(_read_json(f, {}))[0] if f and f.is_file() else None


# ---------------------------------------------------------------- секреты

def cred_file() -> Path:
    return aurora_home() / "git" / "credentials.json"


def project_key(project) -> str:
    return str(Path(project).resolve())


def cred_load(project) -> dict:
    data = _read_json(cred_file(), {})
    got = data.get(project_key(project)) if isinstance(data.get(project_key(project)), dict) else {}
    c = {**CRED_DEFAULT, **{k: str(v) for k, v in got.items() if k in CRED_DEFAULT}}
    if c["auth"] not in AUTHS:
        c["auth"] = "system"
    if c["secret"]:
        _SECRETS.add(c["secret"])
    return c


def cred_masked(project) -> dict:
    c = cred_load(project)
    return {**c, "secret": MASK if c["secret"] else ""}


def cred_merge(project, given: dict | None) -> tuple:
    """(учётные данные, замечания): маска — «не трогали», пусто — «убрать», вставленный
    ключ — в файл с правами 600 вне проекта."""
    old = cred_load(project)
    g = given if isinstance(given, dict) else {}
    c = dict(old)
    probs = []
    auth = str(g.get("auth") or old["auth"])
    c["auth"] = auth if auth in AUTHS else "system"
    if "user" in g:
        c["user"] = " ".join(str(g.get("user") or "").split())
    if "secret" in g:
        sec = str(g.get("secret") or "")
        c["secret"] = old["secret"] if sec == MASK else sec.strip()
    if "ssh_key" in g:
        key = str(g.get("ssh_key") or "").strip()
        c["ssh_key"] = os.path.expanduser(key) if key else ""
    text = str(g.get("ssh_key_text") or "").strip()
    if text:
        if "PRIVATE KEY" not in text:
            probs.append({"field": "ssh_key_text", "code": "key_format"})
        else:
            c["ssh_key"] = str(_store_key(project, text))
    if c["auth"] in ("token", "password") and not c["secret"]:
        probs.append({"field": "secret", "code": "required"})
    if c["auth"] == "password" and not c["user"]:
        probs.append({"field": "user", "code": "required"})
    if c["auth"] == "ssh" and c["ssh_key"] and not Path(c["ssh_key"]).is_file():
        probs.append({"field": "ssh_key", "code": "missing"})
    return c, probs


def _store_key(project, text: str) -> Path:
    keys = aurora_home() / "git" / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(keys.parent, 0o700)
        os.chmod(keys, 0o700)
    except OSError:
        pass
    path = keys / (hashlib.sha1(project_key(project).encode("utf-8")).hexdigest()[:12] + ".key")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text.replace("\r\n", "\n").strip() + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def cred_save(project, c: dict) -> None:
    path = cred_file()
    data = _read_json(path, {})
    data[project_key(project)] = {k: c.get(k, "") for k in CRED_DEFAULT}
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    _write_json(path, data, private=True)


def masked_settings_view(project) -> dict:
    return {"settings": load(project), "credentials": cred_masked(project),
            "saved": bool(settings_file(project) and settings_file(project).is_file())}


# ---------------------------------------------------------------- адрес сервера

def repo_url(s: dict, c: dict) -> str:
    """Адрес для git: из поля «репозиторий» (полный адрес — как есть) или из сервера и
    пути — по правилам провайдера, если его модуль стоит."""
    repo = s.get("repo") or ""
    if not repo:
        return ""
    if is_url(repo):
        return repo
    adapter = load_adapter(s.get("provider", "generic"))
    want = "ssh" if c.get("auth") == "ssh" else "http"
    if adapter:
        try:
            urls = adapter.clone_urls({"instance": s.get("instance", ""), "repo": repo})
            if urls.get(want):
                return urls[want]
        except Exception:                                    # noqa: BLE001
            pass
    inst = s.get("instance") or ""
    if not inst:
        return ""
    if want == "ssh":
        return f"git@{_host(inst)}:{repo}.git"
    return f"{inst}/{repo}.git"


def apply_to_repo(project, s: dict, c: dict) -> list:
    """Записать в репозиторий то, что git хранит сам: адрес сервера и автора. → заметки."""
    notes = []
    url = repo_url(s, c)
    remotes = git_ok(project, "remote").split()
    if url:
        cur = git_ok(project, "remote", "get-url", s["remote"]) if s["remote"] in remotes else ""
        # Адрес с паролем внутри не перезаписываем молча: пароль переезжает отдельным
        # действием, и человек видит, куда он делся.
        if cur and strip_userinfo(cur) == url:
            pass
        elif cur:
            run_git(project, ["remote", "set-url", s["remote"], url])
            notes.append(f"адрес сервера {s['remote']} → {url}")
        else:
            run_git(project, ["remote", "add", s["remote"], url])
            notes.append(f"добавлен сервер {s['remote']} → {url}")
    for key, val in (("user.name", s.get("author_name")), ("user.email", s.get("author_email"))):
        if val and git_ok(project, "config", "--local", key) != val:
            run_git(project, ["config", "--local", key, val])
    return notes


def save(project, settings: dict, creds: dict | None = None) -> dict:
    """Сохранить настройку проекта и учётные данные. Есть замечания — не сохраняется ничего."""
    s, probs = normalize(settings)
    c, cprobs = cred_merge(project, creds)
    probs += cprobs
    if c["auth"] == "token" and not c["user"] and s["provider"] not in ("gitlab",) \
            and not (s["provider"] == "bitbucket" and "bitbucket.org" in s["instance"]):
        probs.append({"field": "user", "code": "required_for_token"})
    if probs:
        return {"ok": False, "problems": probs}
    d = store_dir(project)
    if not d:
        return {"ok": False, "problems": [{"field": "", "code": "no_repo"}]}
    _write_json(d / "git.json", s)
    cred_save(project, c)
    notes = apply_to_repo(project, s, c)
    return {"ok": True, "notes": notes, "settings": s, "credentials": cred_masked(project)}


# ---------------------------------------------------------------- журнал и состояние

def read_state(project) -> dict:
    d = store_dir(project)
    return _read_json(d / "state.json", {}) if d else {}


def write_state(project, **kv) -> None:
    d = store_dir(project)
    if not d:
        return
    st = _read_json(d / "state.json", {})
    st.update(kv)
    try:
        _write_json(d / "state.json", st)
    except OSError:
        pass


def record(project, res: dict, trigger: str = "manual") -> None:
    """Каждое действие — строкой в журнал проекта: автоматика не молчит ни в удаче, ни
    в отказе. Последнее по каждому действию — отдельно, для отметки в меню."""
    d = store_dir(project)
    if not d:
        return
    prob = res.get("problem") or {}
    entry = {"at": utc_iso(), "action": res.get("action", ""), "trigger": trigger,
             "ok": bool(res.get("ok")), "skipped": res.get("skipped", ""),
             "summary": res.get("summary", ""), "code": prob.get("code", "")}
    # Числа итога — отдельно от фразы: панель пишет итог на языке интерфейса по ним.
    for k in ("behind", "ahead", "files", "pushed", "commit", "nothing", "fix"):
        if k in res:
            entry[k] = res[k]
    try:
        d.mkdir(parents=True, exist_ok=True)
        log = d / "log.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        lines.append(json.dumps(entry, ensure_ascii=False))
        log.write_text("\n".join(lines[-LOG_KEEP:]) + "\n", encoding="utf-8")
        last = _read_json(d / "last.json", {})
        last[entry["action"]] = {**entry, "problem": prob or None}
        _write_json(d / "last.json", last)
    except OSError:
        pass


def read_log(project, limit: int = 30) -> list:
    d = store_dir(project)
    log = d / "log.jsonl" if d else None
    if not log or not log.is_file():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return list(reversed(out))


def read_last(project) -> dict:
    d = store_dir(project)
    return _read_json(d / "last.json", {}) if d else {}


def alert(project) -> dict | None:
    """Последняя автоматика упала и после неё удачи не было → что показать в меню.

    Зовётся на каждый проект при каждом списке проектов: читает файл напрямую, без
    запуска git (папка `.git` проекта — обычный случай; рабочее дерево с файлом-указателем
    `.git` отметки не получит, но и автоматика там та же)."""
    last = _read_json(Path(project) / ".git" / "aurora" / "last.json", {}) \
        if (Path(project) / ".git").is_dir() else {}
    bad = [v for v in last.values() if isinstance(v, dict) and not v.get("ok")
           and v.get("trigger") not in ("manual", None)]
    return max(bad, key=lambda v: v.get("at", "")) if bad else None


def provider_quick(project) -> str:
    """Провайдер из сохранённой настройки — чтением файла, без git (для списков проектов)."""
    data = _read_json(Path(project) / ".git" / "aurora" / "git.json", {}) \
        if (Path(project) / ".git").is_dir() else {}
    prov = str(data.get("provider") or "")
    return prov if prov in PROVIDERS else ""


class OpLock:
    """Одно git-действие на проект за раз: два тика или тик и кнопка иначе толкались бы
    в одном индексе."""

    def __init__(self, project, action: str):
        self.path = (store_dir(project) or Path(project)) / "op.lock"
        self.action = action
        self.ok = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "at": time.time(), "action": self.action}, f)
                self.ok = True
                return self
            except FileExistsError:
                held = _read_json(self.path, {})
                if time.time() - float(held.get("at") or 0) > LOCK_STALE_S or not _alive(held.get("pid")):
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                return self
        return self

    def __exit__(self, *exc):
        if self.ok:
            try:
                os.remove(self.path)
            except OSError:
                pass


def _alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- состояние

def operation(gd: Path | None) -> str:
    if not gd:
        return ""
    if (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists():
        return "rebase"
    if (gd / "MERGE_HEAD").exists():
        return "merge"
    if (gd / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return ""


def rebasing_branch(gd: Path | None) -> str:
    """Ветка, которую сейчас переносят: git держит её имя в своей папке переноса."""
    for sub in ("rebase-merge", "rebase-apply"):
        f = gd / sub / "head-name" if gd else None
        if f and f.is_file():
            return f.read_text(encoding="utf-8").strip().removeprefix("refs/heads/")
    return ""


def unborn(project) -> bool:
    """Ветка без единой фиксации: свежий клон или новый проект."""
    return not git_ok(project, "rev-parse", "--verify", "--quiet", "HEAD")


def parse_status(raw: str) -> dict:
    """`git status --porcelain=v2 --branch -z` → ветка, расхождение, изменения по группам."""
    out = {"head": "", "branch": "", "upstream": "", "ahead": 0, "behind": 0,
           "staged": [], "unstaged": [], "untracked": [], "conflicts": []}
    fields = raw.split("\0")
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if not f:
            continue
        if f.startswith("# branch.oid "):
            out["head"] = f.split(" ", 2)[2]
        elif f.startswith("# branch.head "):
            b = f.split(" ", 2)[2]
            out["branch"] = "" if b == "(detached)" else b
        elif f.startswith("# branch.upstream "):
            out["upstream"] = f.split(" ", 2)[2]
        elif f.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", f)
            if m:
                out["ahead"], out["behind"] = int(m.group(1)), int(m.group(2))
        elif f.startswith("1 "):
            parts = f.split(" ", 8)
            _add(out, parts[1], parts[8])
        elif f.startswith("2 "):
            parts = f.split(" ", 9)
            _add(out, parts[1], parts[9])
            i += 1                               # следом — прежнее имя файла
        elif f.startswith("u "):
            parts = f.split(" ", 10)
            out["conflicts"].append(parts[10])
        elif f.startswith("? "):
            out["untracked"].append(f[2:])
    return out


def _add(out: dict, xy: str, path: str) -> None:
    if xy[0] != ".":
        out["staged"].append(path)
    if len(xy) > 1 and xy[1] != ".":
        out["unstaged"].append(path)


def conflict_files(project) -> list:
    out = git_ok(project, "diff", "--name-only", "--diff-filter=U")
    return [x for x in out.splitlines() if x.strip()]


def ref_exists(project, ref: str) -> bool:
    rc, _, _ = run_git(project, ["rev-parse", "--verify", "--quiet", ref])
    return rc == 0


def count(project, rng: str) -> int:
    out = git_ok(project, "rev-list", "--count", rng)
    return int(out) if out.isdigit() else 0


def status(project, fetch: bool = False) -> dict:
    """Всё, что человеку нужно знать о git проекта, одним ответом."""
    project = str(Path(project).resolve())
    if not has_git():
        return {"repo": False, "problems": [problem("no_git")]}
    gd = git_dir(project)
    if not gd:
        return {"repo": False, "problems": [problem("no_repo")]}
    s, c = load(project), cred_load(project)
    saved = bool(settings_file(project) and settings_file(project).is_file())
    remotes = git_ok(project, "remote").split()
    remote = s["remote"]
    probs = []
    fetched = None
    if fetch and remote in remotes:
        rc, o, e = run_git(project, ["fetch", "--prune", remote], s, c, NET_TIMEOUT, network=True)
        if rc:
            probs.append(problem(classify(o + "\n" + e), o + "\n" + e, level="error",
                                 source="fetch"))
        else:
            write_state(project, fetched_at=time.time())
        fetched = rc == 0
    rc, raw, err = run_git(project, ["status", "--porcelain=v2", "--branch", "-z"])
    st = parse_status(raw) if rc == 0 else parse_status("")
    op = operation(gd)
    branch = st["branch"] or (rebasing_branch(gd) if op == "rebase" else "")
    want = s["branch"] or branch
    url = git_ok(project, "remote", "get-url", remote) if remote in remotes else ""
    tracking = f"refs/remotes/{remote}/{want}" if want else ""
    on_server = bool(tracking and ref_exists(project, tracking))
    ahead, behind = st["ahead"], st["behind"]
    if not st["upstream"] and on_server:
        ahead = count(project, f"{tracking}..HEAD")
        behind = count(project, f"HEAD..{tracking}")
    last = {}
    if st["head"] and st["head"] != "(initial)":
        line = git_ok(project, "log", "-1", "--format=%h%x1f%s%x1f%an%x1f%cI")
        parts = line.split("\x1f")
        if len(parts) == 4:
            last = {"hash": parts[0], "subject": parts[1], "author": parts[2], "when": parts[3]}
    # Что мешает работать — по важности: сначала то, без чего ничего не выйдет.
    if not remotes or remote not in remotes:
        probs.append(problem("no_remote", level="error"))
    elif url != strip_userinfo(url) and url_userinfo(url)[1]:
        probs.append(problem("creds_in_url", level="warn"))
    if not branch and not op:
        probs.append(problem("detached", level="error"))
    elif s["branch"] and branch != s["branch"]:
        probs.append(problem("other_branch", level="warn", current=branch, wanted=s["branch"]))
    if op or st["conflicts"]:
        probs.append(problem("conflict" if st["conflicts"] else "in_progress", level="error",
                             files=st["conflicts"], operation=op))
    if remote in remotes and branch and not st["upstream"] and not op:
        probs.append(problem("no_upstream", level="info"))
    if not last:
        probs.append(problem("no_commits", level="info"))
    mod = module_state(s["provider"])
    if mod["state"] in ("missing", "outdated"):
        probs.append(problem("module_" + mod["state"], level="info", module=mod))
    return {
        "repo": True, "project": project, "saved": saved, "provider": s["provider"],
        "branch": branch, "wanted_branch": want, "upstream": st["upstream"],
        "remote": remote, "remotes": remotes, "remote_url": strip_userinfo(url),
        "on_server": on_server, "ahead": ahead, "behind": behind,
        "staged": st["staged"][:LIST_KEEP], "unstaged": st["unstaged"][:LIST_KEEP],
        "untracked": st["untracked"][:LIST_KEEP], "conflicts": st["conflicts"][:LIST_KEEP],
        "counts": {k: len(st[k]) for k in ("staged", "unstaged", "untracked", "conflicts")},
        "operation": op, "last_commit": last, "fetched": fetched,
        "fetched_at": read_state(project).get("fetched_at"),
        "module": mod, "problems": probs, "auth": c["auth"],
    }


def has_changes(project) -> bool:
    return bool(git_ok(project, "status", "--porcelain"))


# ---------------------------------------------------------------- действия

def _context(project, action: str):
    """Общие проверки перед действием → (s, c, branch, remote) или результат с причиной."""
    if not has_git():
        return result(action, False, prob=problem("no_git"))
    if not git_dir(project):
        return result(action, False, prob=problem("no_repo"))
    s, c = load(project), cred_load(project)
    f = settings_file(project)
    if f and f.is_file():
        _, probs = normalize(_read_json(f, {}))
        if probs:
            return result(action, False, prob=problem("settings", fields=probs))
    gd = git_dir(project)
    if action != "fix" and (operation(gd) or conflict_files(project)):
        # Посреди переноса git отцепляет проект от ветки: «не на ветке» здесь была бы
        # неправдой — правда в том, что прошлое обновление не закончено.
        return result(action, False, prob=problem("in_progress", files=conflict_files(project),
                                                  operation=operation(gd)))
    branch = git_ok(project, "branch", "--show-current")
    if not branch:
        return result(action, False, prob=problem("detached"))
    if s["branch"] and branch != s["branch"]:
        return result(action, False, prob=problem("other_branch", current=branch,
                                                  wanted=s["branch"]))
    if s["remote"] not in git_ok(project, "remote").split():
        return result(action, False, prob=problem("no_remote"))
    return s, c, branch, s["remote"]


def render_message(template: str, project, branch: str, files: list, trigger: str) -> str:
    # Дата и время в сообщении фиксации — для человека: в его часах, как он их видит.
    from aurora_common import local_now
    now = local_now()
    vals = {"project": project_name(project), "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"), "branch": branch, "count": str(len(files)),
            "files": ", ".join(Path(f).name for f in files[:3]) + (" …" if len(files) > 3 else ""),
            "trigger": TRIGGER_WORDS.get(trigger, trigger)}
    out = re.sub(r"\{(\w+)\}", lambda m: vals.get(m.group(1), m.group(0)), template or "")
    return " ".join(out.split()) or f"Aurora: {vals['project']}"


def commit_code(tail: str) -> str:
    """Почему не прошла фиксация: хуки Авроры узнаются по своим словам, остальное — по git."""
    if "AURORA_SKIP_RATCHET" in tail:
        return "ratchet"
    if "внутренние названия" in tail:
        return "msg_hook"
    return classify(tail)


def _commit(project, s: dict, message: str | None, skip_ratchet: bool, trigger: str,
            action: str = "commit") -> dict:
    rc, raw, _ = run_git(project, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    st = parse_status(raw)
    files = sorted(set(st["staged"] + st["unstaged"] + st["untracked"]))
    if not files:
        return result(action, True, "нечего фиксировать: изменений нет", nothing=True)
    msg = render_message(message or s["message"], project, s["branch"] or
                         git_ok(project, "branch", "--show-current"), files, trigger)
    rc, o, e = run_git(project, ["add", "-A"])
    if rc:
        return result(action, False, prob=problem(classify(o + e), o + "\n" + e))
    cfg = []
    if s.get("author_name"):
        cfg += ["-c", f"user.name={s['author_name']}"]
    if s.get("author_email"):
        cfg += ["-c", f"user.email={s['author_email']}"]
    # Хуки (храповик, внутренние названия) — Python и по-русски: локаль не трогаем.
    extra = {"AURORA_SKIP_RATCHET": "1"} if skip_ratchet else None
    rc, o, e = run_git(project, [*cfg, "commit", "-m", msg], timeout=600, localized=True,
                       extra_env=extra)
    tail = (o + "\n" + e).strip()
    if rc:
        return result(action, False, prob=problem(commit_code(tail), tail))
    short = git_ok(project, "rev-parse", "--short", "HEAD")
    return result(action, True, f"зафиксировано {short}: «{msg}» (файлов: {len(files)})",
                  commit=short, message=msg, files=len(files))


def commit(project, message: str | None = None, skip_ratchet: bool = False,
           trigger: str = "manual") -> dict:
    project = str(Path(project).resolve())
    if not git_dir(project):
        return _done(project, result("commit", False, prob=problem("no_repo")), trigger)
    with OpLock(project, "commit") as lk:
        if not lk.ok:
            return _done(project, result("commit", False, prob=problem("busy")), trigger)
        return _done(project, _commit(project, load(project), message, skip_ratchet, trigger),
                     trigger)


def _done(project, res: dict, trigger: str) -> dict:
    record(project, res, trigger)
    return res


def _fetch(project, s, c, remote, branch) -> tuple:
    rc, o, e = run_git(project, ["fetch", remote, branch], s, c, NET_TIMEOUT, network=True)
    if rc == 0:
        write_state(project, fetched_at=time.time())
    return rc, (o + "\n" + e).strip()


def _ensure_upstream(project, remote: str, branch: str) -> None:
    if not git_ok(project, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") \
            and ref_exists(project, f"refs/remotes/{remote}/{branch}"):
        run_git(project, ["branch", f"--set-upstream-to={remote}/{branch}"])


def _update(project, strategy: str | None, trigger: str, commit_first: bool,
            action: str = "update") -> dict:
    ctx = _context(project, action)
    if isinstance(ctx, dict):
        return ctx
    s, c, branch, remote = ctx
    gd = git_dir(project)
    if operation(gd) or conflict_files(project):
        return result(action, False, prob=problem("in_progress", files=conflict_files(project),
                                                  operation=operation(gd)))
    if commit_first and has_changes(project):
        r = _commit(project, s, None, False, trigger, action)
        if not r["ok"]:
            return r
    rc, tail = _fetch(project, s, c, remote, branch)
    if rc:
        code = classify(tail)
        if code == "remote_branch_missing":
            return result(action, True, "на сервере этой ветки ещё нет — обновлять не из чего; "
                                        "отправка заведёт её", nothing=True)
        return result(action, False, prob=problem(code, tail))
    target = f"refs/remotes/{remote}/{branch}"
    if not ref_exists(project, target):
        return result(action, True, "на сервере этой ветки ещё нет — обновлять не из чего",
                      nothing=True)
    if unborn(project):
        # Своих фиксаций ещё нет: проект просто встаёт на серверную историю. Свои файлы,
        # которые совпали бы с серверными по имени, git не тронет — скажет, какие мешают.
        behind = count(project, target)
        rc, o, e = run_git(project, ["merge", "--ff-only", target], timeout=600)
        if rc:
            tail = (o + "\n" + e).strip()
            return result(action, False, prob=problem(classify(tail), tail))
        _ensure_upstream(project, remote, branch)
        files = git_ok(project, "ls-files").splitlines()
        return result(action, True, f"получено с сервера изменений: {behind}, файлов: {len(files)}",
                      behind=behind, files=len(files))
    behind = count(project, f"HEAD..{target}")
    ahead = count(project, f"{target}..HEAD")
    _ensure_upstream(project, remote, branch)
    if behind == 0:
        return result(action, True, "уже актуально: новых изменений на сервере нет",
                      nothing=True, ahead=ahead)
    strat = strategy if strategy in STRATEGIES else s["strategy"]
    if strat == "ff-only" and ahead:
        return result(action, False, prob=problem("diverged", ahead=ahead, behind=behind))
    before = git_ok(project, "rev-parse", "HEAD")
    if strat == "rebase":
        args = ["rebase", "--autostash", target]
    elif strat == "merge":
        args = ["merge", "--no-edit", "--autostash", target]
    else:
        args = ["merge", "--ff-only", "--autostash", target]
    rc, o, e = run_git(project, args, timeout=600)
    tail = (o + "\n" + e).strip()
    files = conflict_files(project)
    if rc or files:
        code = "conflict" if files or operation(gd) else classify(tail)
        return result(action, False, prob=problem(code, tail, files=files,
                                                  operation=operation(gd)))
    changed = git_ok(project, "diff", "--name-only", before, "HEAD").splitlines()
    word = {"ff-only": "перемоткой", "merge": "слиянием", "rebase": "переносом своих поверх"}[strat]
    return result(action, True, f"получено с сервера изменений: {behind}, файлов затронуто: "
                                f"{len(changed)} ({word})", behind=behind, files=len(changed))


def update(project, strategy: str | None = None, trigger: str = "manual",
           commit_first: bool = False) -> dict:
    project = str(Path(project).resolve())
    if not git_dir(project):
        return _done(project, result("update", False, prob=problem("no_repo")), trigger)
    with OpLock(project, "update") as lk:
        if not lk.ok:
            return _done(project, result("update", False, prob=problem("busy")), trigger)
        res = _update(project, strategy, trigger, commit_first)
        if trigger != "manual":
            write_state(project, **({"auto_update_fail_at": time.time()} if not res["ok"]
                                    else {"auto_update_fail_at": 0}))
        return _done(project, res, trigger)


def _push(project, message, do_commit, trigger, update_first, skip_ratchet) -> dict:
    ctx = _context(project, "push")
    if isinstance(ctx, dict):
        return ctx
    s, c, branch, remote = ctx
    gd = git_dir(project)
    if operation(gd) or conflict_files(project):
        return result("push", False, prob=problem("in_progress", files=conflict_files(project),
                                                  operation=operation(gd)))
    notes = []
    if update_first:
        r = _update(project, None, trigger, False, action="push")
        if not r["ok"]:
            return r
        notes.append(r["summary"])
    if do_commit and s.get("commit_on_push", True) and has_changes(project):
        r = _commit(project, s, message, skip_ratchet, trigger, action="push")
        if not r["ok"]:
            return r
        notes.append(r["summary"])
    if not git_ok(project, "rev-parse", "--verify", "--quiet", "HEAD"):
        return result("push", False, prob=problem("no_commits"))
    target = f"refs/remotes/{remote}/{branch}"
    upstream = git_ok(project, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    known = ref_exists(project, target)
    ahead = count(project, f"{target}..HEAD") if known else count(project, "HEAD")
    if known and upstream and ahead == 0:
        write_state(project, pushed_head=git_ok(project, "rev-parse", "HEAD"))
        return result("push", True, "; ".join(notes + ["всё уже на сервере: отправлять нечего"]),
                      nothing=True)
    args = ["push", remote, f"HEAD:refs/heads/{branch}"]
    if not upstream:
        args.insert(1, "--set-upstream")
    rc, o, e = run_git(project, args, s, c, NET_TIMEOUT, network=True)
    tail = (o + "\n" + e).strip()
    if rc:
        return result("push", False, "; ".join(notes), prob=problem(classify(tail), tail))
    _ensure_upstream(project, remote, branch)
    # Только что говорили с сервером — его состояние известно на эту минуту.
    write_state(project, pushed_head=git_ok(project, "rev-parse", "HEAD"), fetched_at=time.time())
    return result("push", True, "; ".join(notes + [f"отправлено фиксаций: {ahead} → "
                                                   f"{remote}/{branch}"]), pushed=ahead)


def push(project, message: str | None = None, do_commit: bool = True, trigger: str = "manual",
         update_first: bool = False, skip_ratchet: bool = False) -> dict:
    project = str(Path(project).resolve())
    if not git_dir(project):
        return _done(project, result("push", False, prob=problem("no_repo")), trigger)
    with OpLock(project, "push") as lk:
        if not lk.ok:
            return _done(project, result("push", False, prob=problem("busy")), trigger)
        res = _push(project, message, do_commit, trigger, update_first, skip_ratchet)
        if trigger != "manual":
            write_state(project, **({"auto_push_fail_at": time.time()} if not res["ok"]
                                    else {"auto_push_fail_at": 0}))
        return _done(project, res, trigger)


def fix(project, what: str, file: str = "", trigger: str = "manual") -> dict:
    """Починка одним действием — то, что панель предлагает рядом с причиной."""
    project = str(Path(project).resolve())
    if what not in FIXES:
        return result("fix", False, prob=problem("unknown", f"нет такого действия: {what}"))
    if what == "init":
        return _done(project, _fix_init(project), trigger)
    gd = git_dir(project)
    if not gd:
        return _done(project, result("fix", False, prob=problem("no_repo")), trigger)
    with OpLock(project, "fix") as lk:
        if not lk.ok:
            return _done(project, result("fix", False, prob=problem("busy")), trigger)
        res = _fix(project, gd, what, file)
        res["fix"] = what
        return _done(project, res, trigger)


def _fix_init(project) -> dict:
    if git_dir(project):
        return result("fix", True, "репозиторий уже есть", nothing=True)
    branch = "main"
    rc, o, e = run_git(project, ["init", "-b", branch])
    if rc:
        rc, o, e = run_git(project, ["init"])
    if rc:
        return result("fix", False, prob=problem(classify(o + e), o + "\n" + e))
    return result("fix", True, f"создан репозиторий (ветка {branch})")


def _fix(project, gd: Path, what: str, file: str) -> dict:
    s, c = load(project), cred_load(project)
    op = operation(gd)
    if what == "abort":
        if not op:
            return result("fix", True, "отменять нечего: обновление не идёт", nothing=True)
        args = ["rebase", "--abort"] if op == "rebase" else ["merge", "--abort"] if op == "merge" \
            else ["cherry-pick", "--abort"]
        rc, o, e = run_git(project, args)
        return result("fix", rc == 0, "обновление отменено: проект как до него" if rc == 0 else "",
                      prob=None if rc == 0 else problem(classify(o + e), o + "\n" + e))
    if what in ("mine", "theirs", "resolved"):
        files = conflict_files(project)
        if file not in files:
            return result("fix", False, prob=problem("conflict", f"файл не в конфликте: {file}",
                                                     files=files))
        if what == "resolved":
            text = ""
            try:
                text = (Path(project) / file).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            if re.search(r"^(<{7}|>{7})( |$)", text, re.M):
                return result("fix", False, prob=problem("conflict_markers", files=[file]))
        else:
            # При переносе своих поверх серверных git зовёт серверную версию «нашей».
            side = {"mine": "--theirs", "theirs": "--ours"} if op == "rebase" \
                else {"mine": "--ours", "theirs": "--theirs"}
            rc, o, e = run_git(project, ["checkout", side[what], "--", file])
            if rc:
                # У этой стороны файла нет (удалён) — значит, выбрать её = удалить файл.
                rc, o, e = run_git(project, ["rm", "-q", "--", file])
                if rc:
                    return result("fix", False, prob=problem(classify(o + e), o + "\n" + e))
                left = conflict_files(project)
                return result("fix", True, f"{file}: принято удаление", left=left)
        rc, o, e = run_git(project, ["add", "--", file])
        if rc:
            return result("fix", False, prob=problem(classify(o + e), o + "\n" + e))
        left = conflict_files(project)
        word = {"mine": "оставлена ваша версия", "theirs": "взята версия сервера",
                "resolved": "принята ваша правка"}[what]
        return result("fix", True, f"{file}: {word}; осталось файлов в конфликте: {len(left)}",
                      left=left)
    if what == "continue":
        left = conflict_files(project)
        if left:
            return result("fix", False, prob=problem("conflict", files=left, operation=op))
        if op == "rebase":
            rc, o, e = run_git(project, ["rebase", "--continue"], timeout=600)
        elif op == "merge":
            rc, o, e = run_git(project, ["commit", "--no-edit"], timeout=600, localized=True)
        else:
            return result("fix", True, "завершать нечего", nothing=True)
        if rc:
            files = conflict_files(project)
            code = "conflict" if files else commit_code(o + "\n" + e)
            return result("fix", False, prob=problem(code, o + "\n" + e, files=files,
                                                     operation=operation(gd)))
        return result("fix", True, "обновление завершено")
    if what == "set-upstream":
        branch = git_ok(project, "branch", "--show-current")
        remote = s["remote"]
        if not branch:
            return result("fix", False, prob=problem("detached"))
        if ref_exists(project, f"refs/remotes/{remote}/{branch}"):
            rc, o, e = run_git(project, ["branch", f"--set-upstream-to={remote}/{branch}"])
            return result("fix", rc == 0, f"ветка {branch} связана с {remote}/{branch}" if rc == 0
                          else "", prob=None if rc == 0 else problem(classify(o + e), o + e))
        res = _push(project, None, False, "manual", False, False)
        res["action"] = "fix"
        return res
    if what == "checkout":
        want = s["branch"]
        if not want:
            return result("fix", False, prob=problem("settings", fields=[{"field": "branch",
                                                                          "code": "required"}]))
        if ref_exists(project, f"refs/heads/{want}"):
            rc, o, e = run_git(project, ["checkout", want])
        elif ref_exists(project, f"refs/remotes/{s['remote']}/{want}"):
            rc, o, e = run_git(project, ["checkout", "-b", want, "--track", f"{s['remote']}/{want}"])
        else:
            rc, o, e = run_git(project, ["checkout", "-b", want])
        return result("fix", rc == 0, f"открыта ветка {want}" if rc == 0 else "",
                      prob=None if rc == 0 else problem(classify(o + e), o + "\n" + e))
    if what == "use-branch":
        branch = git_ok(project, "branch", "--show-current")
        if not branch:
            return result("fix", False, prob=problem("detached"))
        f = settings_file(project)
        data = _read_json(f, {}) if f and f.is_file() else load(project)
        data["branch"] = branch
        _write_json(store_dir(project) / "git.json", normalize(data)[0])
        return result("fix", True, f"ветка проекта теперь {branch}")
    if what == "strip-url":
        remote = s["remote"]
        url = git_ok(project, "remote", "get-url", remote)
        user, pw = url_userinfo(url)
        if not user and not pw:
            return result("fix", True, "в адресе нет пароля", nothing=True)
        if pw:
            cred_save(project, {**c, "auth": "password" if c["auth"] in ("system", "password")
                                else c["auth"], "user": user or c["user"], "secret": pw})
        run_git(project, ["remote", "set-url", remote, strip_userinfo(url)])
        return result("fix", True, "пароль убран из адреса" + (" и перенесён в защищённое "
                                                               "хранилище" if pw else ""))
    if what == "fix-key-perms":
        key = c.get("ssh_key")
        if not key or not Path(key).is_file():
            return result("fix", False, prob=problem("ssh_key"))
        try:
            os.chmod(key, 0o600)
        except OSError as e:
            return result("fix", False, prob=problem("unknown", str(e)))
        return result("fix", True, "ключ закрыт правами 600")
    return result("fix", False, prob=problem("unknown"))


# ---------------------------------------------------------------- проверка подключения

def tls_context(tls: dict | None):
    tls = tls or {}
    if tls.get("verify") is False:
        return ssl._create_unverified_context()              # noqa: S323 — выбор человека
    if tls.get("ca_file"):
        return ssl.create_default_context(cafile=tls["ca_file"])
    return ssl.create_default_context()


def make_http(tls: dict | None):
    """Помощник HTTP для адаптеров: (код, json|None, ошибка, заголовки). Код 0 — до
    сервера не дошли; ошибка тогда начинается с «cert:» или «network:»."""
    ctx = tls_context(tls)

    def http(url: str, headers: dict | None = None, timeout: int = 20):
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "aurora-studio",
                                                   **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                body = r.read(3_000_000)
                hdrs = {k.lower(): v for k, v in r.headers.items()}
                try:
                    return r.status, json.loads(body.decode("utf-8", "replace") or "null"), "", hdrs
                except ValueError:
                    return r.status, None, "не JSON", hdrs
        except urllib.error.HTTPError as e:
            return e.code, None, f"HTTP {e.code}", {k.lower(): v for k, v in (e.headers or {}).items()}
        except urllib.error.URLError as e:
            why = str(e.reason)
            if isinstance(e.reason, ssl.SSLError) or "CERTIFICATE" in why.upper():
                return 0, None, "cert: " + why, {}
            return 0, None, "network: " + why, {}
        except (socket.timeout, TimeoutError, OSError) as e:
            return 0, None, "network: " + str(e), {}
    return http


def check(project, settings: dict | None = None, creds: dict | None = None) -> dict:
    """Проверка подключения по настройке (в том числе ещё не сохранённой): API провайдера,
    если его модуль стоит, и сам git — `ls-remote` с тем же входом."""
    project = str(Path(project).resolve())
    s = normalize(settings)[0] if settings is not None else load(project)
    tmp_key = None
    if creds is not None:
        given = dict(creds)
        text = str(given.pop("ssh_key_text", "") or "").strip()
        c = cred_merge(project, given)[0]
        if text:
            # Проверка не сохраняет ничего: вставленный ключ живёт во временном файле.
            tmp_key = aurora_home() / "git" / "keys" / f"check-{os.getpid()}.key"
            tmp_key.parent.mkdir(parents=True, exist_ok=True)
            tmp_key.write_text(text.replace("\r\n", "\n").strip() + "\n", encoding="utf-8")
            os.chmod(tmp_key, 0o600)
            c["ssh_key"] = str(tmp_key)
    else:
        c = cred_load(project)
    try:
        return _check(project, s, c)
    finally:
        if tmp_key:
            try:
                tmp_key.unlink()
            except OSError:
                pass


def _check(project, s: dict, c: dict) -> dict:
    steps, prob, suggest = [], None, {}
    url = repo_url(s, c)
    steps.append({"id": "url", "ok": bool(url), "detail": strip_userinfo(url)})
    if not url:
        return {"ok": False, "steps": steps, "problem": problem("no_remote"), "suggest": suggest}
    adapter = load_adapter(s["provider"])
    mod = module_state(s["provider"])
    path = s["repo"] if not is_url(s["repo"]) else ""
    if adapter and path:
        cfg = {"instance": s["instance"], "repo": path, "auth": c["auth"],
               "user": c["user"], "secret": c["secret"] if c["auth"] in ("token", "password") else ""}
        try:
            api = adapter.check(cfg, make_http(s["tls"]))
        except Exception as e:                               # noqa: BLE001
            api = {"ok": False, "code": "unknown", "detail": f"{type(e).__name__}: {e}"}
        repo = api.get("repo") or {}
        steps.append({"id": "api", "ok": bool(api.get("ok")), "detail": api.get("detail", ""),
                      "login": api.get("login", ""), "repo": repo})
        if api.get("login"):
            suggest["user"] = api["login"]
        if repo.get("default_branch"):
            suggest["branch"] = repo["default_branch"]
        if not api.get("ok") and api.get("code"):
            prob = problem(api["code"], api.get("detail", ""))
    elif s["provider"] != "generic":
        steps.append({"id": "api", "ok": None, "detail": mod.get("state", "")})
    base = s["instance"] or (_origin(url) if URL_RX.match(url) else "")
    if s["provider"] == "generic" and base:
        found = detect_provider(base, s["tls"])
        if found:
            suggest["provider"] = found
    rc, o, e = run_git(project, ["ls-remote", "--heads", url], s, c, 60, network=True)
    tail = (o + "\n" + e).strip()
    heads = [ln.split("refs/heads/", 1)[1] for ln in o.splitlines() if "refs/heads/" in ln]
    steps.append({"id": "git", "ok": rc == 0, "detail": "" if rc == 0 else tail[-400:],
                  "branches": heads[:50]})
    if rc and not prob:
        prob = problem(classify(tail), tail)
    want = s["branch"] or (suggest.get("branch") or "")
    if rc == 0 and want:
        steps.append({"id": "branch", "ok": want in heads, "detail": want})
    if rc == 0 and not heads:
        suggest["empty"] = True
    return {"ok": rc == 0 and (prob is None), "steps": steps, "problem": prob,
            "suggest": suggest, "module": mod}


def _origin(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def detect_provider(base: str, tls: dict | None = None) -> str:
    """Кто отвечает по адресу сервера: Gitea, GitLab или Bitbucket. Нестандартный порт
    своего сервера по имени не угадать — спрашиваем сам сервер его открытыми адресами."""
    http = make_http(tls)
    base = base.rstrip("/")
    st, data, _e, _h = http(base + "/api/v1/version", timeout=8)
    if st == 200 and isinstance(data, dict) and data.get("version"):
        return "gitea"
    st, data, _e, _h = http(base + "/rest/api/1.0/application-properties", timeout=8)
    if st == 200 and isinstance(data, dict) and "bitbucket" in str(data.get("displayName", "")).lower():
        return "bitbucket"
    st, _d, _e, hdrs = http(base + "/api/v4/version", timeout=8)
    if st in (200, 401) and ("x-gitlab-meta" in hdrs or "gitlab" in str(hdrs.get("set-cookie", "")).lower()):
        return "gitlab"
    if "bitbucket.org" in base:
        return "bitbucket"
    return ""


def cert_info(url: str) -> dict:
    """Сертификат сервера — для доверия по отпечатку. Ничего не сохраняет."""
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    host, port = u.hostname, u.port or 443
    if not host:
        return {"ok": False, "error": "в адресе нет сервера"}
    try:
        pem = ssl.get_server_certificate((host, port), timeout=15)
    except TypeError:                                         # Python < 3.10: без timeout
        pem = ssl.get_server_certificate((host, port))
    except (OSError, ssl.SSLError) as e:
        return {"ok": False, "error": f"сервер {host}:{port} не отдал сертификат: {e}"}
    der = ssl.PEM_cert_to_DER_cert(pem)
    fp = hashlib.sha256(der).hexdigest().upper()
    return {"ok": True, "host": host, "port": port, "pem": pem,
            "sha256": ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))}


def trust_cert(project, url: str, sha256: str) -> dict:
    """Доверять сертификату сервера: сохранить его и указать git и API именно его, а не
    выключать проверку. Отпечаток сверяется — подмена между показом и согласием не пройдёт."""
    info = cert_info(url)
    if not info.get("ok"):
        return info
    if info["sha256"].replace(":", "").upper() != (sha256 or "").replace(":", "").upper():
        return {"ok": False, "error": "отпечаток изменился с момента показа — доверие не выдано"}
    certs = aurora_home() / "git" / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    path = certs / f"{info['host']}_{info['port']}.pem"
    path.write_text(info["pem"], encoding="utf-8")
    f = settings_file(project)
    data = _read_json(f, {}) if f and f.is_file() else load(project)
    data["tls"] = {"verify": True, "ca_file": str(path)}
    s = normalize(data)[0]
    if store_dir(project):
        _write_json(store_dir(project) / "git.json", s)
    return {"ok": True, "path": str(path), "sha256": info["sha256"]}


# ---------------------------------------------------------------- командная строка

def _print_result(res: dict) -> None:
    prob = res.get("problem")
    if res.get("ok"):
        print(f"✓ {res.get('summary') or 'готово'}")
        return
    if res.get("summary"):
        print(f"· {res['summary']}")
    print(f"✗ {prob['title']}")
    print(f"  Что сделать: {prob['fix']}")
    files = prob.get("files") or []
    if files:
        print("  Файлы: " + ", ".join(files[:20]) + (" …" if len(files) > 20 else ""))
    for f in prob.get("fields") or []:
        print(f"  Поле настройки: {f.get('field')} — {f.get('code')}")
    if prob.get("raw"):
        print("  Вывод git:")
        for line in prob["raw"].splitlines()[-25:]:
            print("    " + line)


def _print_status(st: dict) -> None:
    if not st.get("repo"):
        for p in st.get("problems", []):
            print(f"✗ {p['title']} — {p['fix']}")
        return
    print(f"Ветка: {st['branch'] or '—'}   сервер: {st['remote']} {st['remote_url'] or '—'}")
    print(f"Связь с сервером: {st['upstream'] or 'нет'}   "
          f"у вас не отправлено: {st['ahead']}   на сервере новых: {st['behind']}")
    cnt = st["counts"]
    print(f"Изменения: подготовлено {cnt['staged']}, изменено {cnt['unstaged']}, "
          f"новых {cnt['untracked']}, в конфликте {cnt['conflicts']}")
    if st.get("last_commit"):
        lc = st["last_commit"]
        print(f"Последняя фиксация: {lc['hash']} «{lc['subject']}» — {lc['author']}, {lc['when']}")
    for p in st.get("problems", []):
        mark = {"error": "✗", "warn": "!", "info": "·"}.get(p.get("level"), "·")
        print(f"{mark} {p['title']} — {p['fix']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Git проекта: состояние, обновление, отправка")
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument("--status", action="store_true", help="состояние git проекта")
    act.add_argument("--update", action="store_true", help="обновить проект с сервера")
    act.add_argument("--push", action="store_true",
                     help="отправить на сервер (незафиксированное сначала фиксируется)")
    act.add_argument("--commit", action="store_true", help="зафиксировать изменения")
    act.add_argument("--fix", action="store_true", help="починка одним действием (что — в --what)")
    act.add_argument("--check", action="store_true", help="проверить вход и репозиторий на сервере")
    act.add_argument("--modules", action="store_true", help="модули провайдеров: что стоит, что в ките")
    ap.add_argument("--fetch", action="store_true", help="со статусом: спросить сервер о новом")
    ap.add_argument("--strategy", choices=STRATEGIES, help="как объединять при обновлении")
    ap.add_argument("--message", help="сообщение фиксации (по умолчанию — шаблон настройки)")
    ap.add_argument("--no-commit", action="store_true", help="отправить без фиксации изменений")
    ap.add_argument("--update-first", action="store_true", help="перед отправкой обновить")
    ap.add_argument("--commit-first", action="store_true", help="перед обновлением зафиксировать")
    ap.add_argument("--skip-ratchet", action="store_true",
                    help="фиксировать, даже если храповик против (решение человека)")
    ap.add_argument("--what", choices=FIXES, help="что чинить: abort, continue, mine, theirs, resolved, "
                    "set-upstream, init, checkout, use-branch, strip-url, fix-key-perms")
    ap.add_argument("--file", default="", help="файл для --what mine|theirs|resolved")
    ap.add_argument("--auto", choices=UPDATE_ON + PUSH_ON,
                    help="запуск автоматикой: работает, только если она включена на это событие")
    ap.add_argument("--project", default=".", help="папка проекта (по умолчанию — текущая)")
    ap.add_argument("--json", action="store_true", help="итог одной строкой JSON")
    a = ap.parse_args(argv)
    if a.fix and not a.what:
        ap.error("--fix: скажите, что чинить, — --what")
    project = str(Path(a.project).resolve())
    trigger = a.auto or "manual"

    if a.modules:
        rows = module_rows()
        if a.json:
            print(json.dumps({"modules": rows}, ensure_ascii=False))
            return 0
        for r in rows:
            state = "не установлен" if r["missing"] else f"стоит {r['installed']}"
            more = f", в ките {r['kit']}" if r["kit"] else ""
            print(f"{r['title']}: {state}{more}" + (" — есть обновление" if r["update"] else ""))
        print("Generic Git: встроен в движок")
        return 0
    if a.status:
        st = status(project, fetch=a.fetch)
        if a.fetch and st.get("repo"):
            # Опрос сервера — сетевое действие: его итог, как и у остальных, в журнал.
            failed = next((p for p in st["problems"] if p.get("source") == "fetch"), None)
            record(project, result("fetch", failed is None,
                                   "" if failed else "сервер опрошен: новых изменений "
                                   f"{st['behind']}, неотправленных {st['ahead']}",
                                   prob=failed, behind=st["behind"], ahead=st["ahead"]),
                   trigger)
        if a.json:
            print(json.dumps(st, ensure_ascii=False))
        else:
            _print_status(st)
        return 0 if st.get("repo") and not any(p.get("level") == "error" for p in st["problems"]) else 1
    if a.check:
        res = check(project)
        if a.json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            for step in res["steps"]:
                mark = "✓" if step["ok"] else ("·" if step["ok"] is None else "✗")
                print(f"{mark} {step['id']}: {step.get('detail') or ''}")
            if res.get("problem"):
                _print_result(result("check", False, prob=res["problem"]))
        return 0 if res["ok"] else 1

    if a.auto:
        s = load_saved(project)
        group = "auto_update" if a.update else "auto_push" if a.push else ""
        if not s or not group or not s[group]["enabled"] or a.auto not in s[group]["on"]:
            print(f"Автоматика «{TRIGGER_WORDS.get(a.auto, a.auto)}» для этого действия "
                  "выключена — ничего не делаю.")
            return 0
        print(f"Автоматика: {TRIGGER_WORDS.get(a.auto, a.auto)}")
    if a.update:
        res = update(project, a.strategy, trigger, a.commit_first)
    elif a.push:
        res = push(project, a.message, not a.no_commit, trigger, a.update_first, a.skip_ratchet)
    elif a.commit:
        res = commit(project, a.message, a.skip_ratchet, trigger)
    else:
        res = fix(project, a.what, a.file, trigger)
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        _print_result(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
