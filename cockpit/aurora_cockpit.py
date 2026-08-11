#!/usr/bin/env python3
"""aurora_cockpit.py — локальный сервер панели управления Aurora.

  python3 cockpit/aurora_cockpit.py            # поднять и открыть в браузере
  python3 cockpit/aurora_cockpit.py --port 8787 --roots ~/work ~/projects

Панель — один self-contained HTML (`cockpit/ui/index.html`), сервер — только стандартная
библиотека: контур закрытый, ставить в него нечего.

Что делает сервер и чего не делает:

* находит проекты Авроры (файл `aurora.config.yaml`) в заданных корнях;
* запускает команды движка **из реестра `commands.txt`** — произвольную строку выполнить
  нельзя, аргументы уходят списком, без оболочки;
* слушает только петлевой интерфейс и требует токен сессии, выданный при старте: браузер
  получает его вместе со страницей, чужая вкладка — нет;
* флаг `--apply` не подставляет сам: его присылает интерфейс после подтверждения человеком;
* секретов не касается — про токены синка знает только «заполнено» или «пусто».
"""
from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import sys
from pathlib import Path
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(KIT, "cockpit", "ui", "index.html")
sys.path.insert(0, os.path.join(KIT, "scripts"))

TOKEN = secrets.token_urlsafe(24)
JOBS: dict = {}
JOBS_LOCK = threading.Lock()
CACHE: dict = {}
REGISTRY_CACHE = os.path.join(KIT, "cockpit", ".registry-cache.json")

# Документы, которые панель имеет право показать. Всё остальное читать нельзя:
# сервер живёт в репозитории с рабочими данными.
DOC_ROOTS = ("docs", "skills/aurora-vault", "CHANGELOG.md", "commands.txt", "README.md")


# ------------------------------------------------------------------- скины

SKINS_DIR = os.path.join(KIT, "cockpit", "skins")


def skins() -> list:
    """Оформление вынесено в файлы: положил свой .css в cockpit/skins/ — он в списке.

    Имя, описание и версия берутся из шапки самого файла (`/* name: … for: … about: … */`),
    чтобы добавление скина не требовало править ни сервер, ни панель.

    `for:` — версия ядра, под которую скин собран. Скин красит то, чего в панели могло
    ещё не быть: новый элемент выйдет в цветах по умолчанию, и понять это по внешнему
    виду нельзя. Если версия не объявлена, считаем скин сегодняшним — так ведут себя
    все скины, написанные до появления поля.
    """
    out = []
    if not os.path.isdir(SKINS_DIR):
        return out
    for f in sorted(os.listdir(SKINS_DIR)):
        if not f.endswith(".css"):
            continue
        head = read_text(os.path.join(SKINS_DIR, f), limit=2000)
        name = (re.search(r"name:\s*(.+)", head) or [None, f[:-4]])[1].strip()
        about = (re.search(r"about:\s*([\s\S]*?)\*/", head) or [None, ""])[1]
        about = " ".join(x.strip() for x in about.splitlines() if x.strip())
        ver = (re.search(r"for:\s*([0-9][0-9.]*)", head) or [None, kit_version()])[1].strip()
        out.append({"id": f[:-4], "name": name, "about": about, "for": ver,
                    "behind": minor(ver) != minor(kit_version())})
    return out


def skin_css(skin_id: str) -> str:
    """Только файлы из cockpit/skins и только .css — путь снаружи не принимается."""
    name = os.path.basename(skin_id) + ".css"
    path = os.path.join(SKINS_DIR, name)
    return read_text(path, limit=200_000) if os.path.isfile(path) else ""


# --------------------------------------------------------------- быстрый старт

def scenarios() -> list:
    """Сценарии рутинной работы: последовательность шагов вместо поиска по 49 командам.

    Лежат в `cockpit/scenarios.txt` — обычный текст, который правит человек, а не код.
    Шаг без команды (строка с «-») — работа человека или ассистента: её панель не
    запускает, но и пропускать её в сценарии нечестно.
    """
    path = os.path.join(KIT, "cockpit", "scenarios.txt")
    out, cur = [], None
    for line in read_text(path).splitlines():
        line = line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"\[([\w-]+)\]\s*([^|]+?)\s*(?:\|\s*(.*))?$", line.strip())
        if m:
            # Третье поле заголовка — вкладка. Обслуживание базы и продуктивность это
            # разные занятия и разные дни: держать их в одном списке значит заставлять
            # искать нужное среди чужого.
            tail = [x.strip() for x in (m.group(3) or "").split("|")]
            cur = {"id": m.group(1), "title": m.group(2).strip(),
                   "when": tail[0] if tail else "",
                   "group": (tail[1] if len(tail) > 1 else "") or "база",
                   "steps": []}
            out.append(cur)
            continue
        if cur is None:
            continue
        parts = [x.strip() for x in line.split("|")]
        if parts[0].startswith("-"):
            # третье поле — команда скилла для ассистента: панель её не запускает,
            # но человек должен видеть, что именно сказать модели
            cur["steps"].append({"manual": True, "title": parts[0].lstrip("- ").strip(),
                                 "why": parts[1] if len(parts) > 1 else "",
                                 "skill": parts[2] if len(parts) > 2 else ""})
        else:
            cur["steps"].append({"manual": False, "cmd": parts[0],
                                 "why": parts[1] if len(parts) > 1 else "",
                                 "flags": (parts[2].split() if len(parts) > 2 else [])})
    return out


# ------------------------------------------------------------------- о проекте

def about() -> dict:
    """Факты о ките: откуда взялся, чья работа, что внутри. Всё — из репозитория."""
    def git(*args, default=""):
        try:
            r = subprocess.run(["git", "-C", KIT, *args], capture_output=True,
                               text=True, timeout=20)
            return r.stdout.strip() if r.returncode == 0 else default
        except Exception:
            return default
    remote = git("remote", "get-url", "origin")
    web = re.sub(r"^git@([^:]+):", r"https://\1/", remote).removesuffix(".git")
    changelog = read_text(os.path.join(KIT, "CHANGELOG.md"), limit=8000)
    head = [l for l in changelog.splitlines() if l.startswith("## ")][:5]
    return {
        "kit": kit_version(), "ui": ui_version(), "path": KIT,
        "repo": web, "branch": git("branch", "--show-current"),
        "commit": git("rev-parse", "--short", "HEAD"),
        "commit_date": git("log", "-1", "--format=%ad", "--date=short"),
        "author": git("log", "--reverse", "--format=%an", "-1") or "—",
        "license": "Apache-2.0" if os.path.isfile(os.path.join(KIT, "LICENSE")) else "—",
        "commands": len(registry()),
        "releases": [h[3:] for h in head],
    }


def kit_git_status() -> dict:
    """Есть ли в репозитории kit'а что-то новое и можно ли обновиться без потерь."""
    def git(*args):
        return subprocess.run(["git", "-C", KIT, *args], capture_output=True,
                              text=True, timeout=120)
    if not os.path.isdir(os.path.join(KIT, ".git")):
        return {"error": "kit не под git — обновление из репозитория недоступно"}
    fetch = git("fetch", "--quiet")
    if fetch.returncode != 0:
        return {"error": "не удалось связаться с репозиторием: "
                         + (fetch.stderr.strip()[-200:] or "нет сети")}
    branch = git("branch", "--show-current").stdout.strip() or "master"
    counts = git("rev-list", "--left-right", "--count", f"{branch}...origin/{branch}").stdout.split()
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    dirty = [l for l in git("status", "--porcelain").stdout.splitlines()
             if l.strip() and "__pycache__" not in l]
    log = git("log", "--oneline", f"HEAD..origin/{branch}").stdout.splitlines()[:10]
    return {"branch": branch, "ahead": ahead, "behind": behind,
            "dirty": len(dirty), "incoming": log, "version": kit_version()}


def kit_pull() -> dict:
    """Обновление kit'а из репозитория. Только перемотка вперёд: слияние с чужими
    правками — не то, что стоит делать кнопкой в браузере."""
    st = kit_git_status()
    if st.get("error"):
        return st
    if st["dirty"]:
        return {"error": f"в kit'е {st['dirty']} незакоммиченных файлов — "
                         "обновление затрёт их. Сначала закоммитьте или отмените правки"}
    if not st["behind"]:
        return {"ok": True, "already": True, "version": kit_version()}
    r = subprocess.run(["git", "-C", KIT, "pull", "--ff-only"], capture_output=True,
                       text=True, timeout=180)
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[-400:]}
    CACHE.pop("registry", None)
    return {"ok": True, "version": kit_version(), "log": r.stdout.strip().splitlines()[-8:]}


# --------------------------------------------------------------- реестр команд

def registry() -> list:
    """Команды из `commands.txt`; модификаторы — из `--help` самих скриптов.

    Kit могли обновить, пока панель работает: тогда в реестре появляются новые команды и
    флаги, а панель показывает вчерашний список. Сторожим по времени правки VERSION —
    дешевле, чем перечитывать `--help` полусотни скриптов на каждый запрос.
    """
    stamp = os.path.getmtime(os.path.join(KIT, "VERSION")) if os.path.isfile(
        os.path.join(KIT, "VERSION")) else 0
    if "registry" in CACHE and CACHE.get("registry_stamp") == stamp:
        return CACHE["registry"]
    CACHE["registry_stamp"] = stamp
    # Кэш на диске: сборка реестра запускает `--help` у полусотни скриптов, и это секунды
    # на КАЖДОМ старте панели — а меняется он только вместе с версией ядра и реестром
    # команд. Ключ — обе метки; не сошлись, значит пересобираем.
    key = f"{kit_version()}|{stamp}|{os.path.getmtime(os.path.join(KIT, 'commands.txt'))}"
    cached = read_text(REGISTRY_CACHE, limit=4_000_000)
    if cached:
        try:
            data = json.loads(cached)
            if data.get("key") == key:
                CACHE["registry"] = data["rows"]
                return data["rows"]
        except (ValueError, KeyError):
            pass
    import kit_commands as K
    from concurrent.futures import ThreadPoolExecutor
    entries = K.read_registry()
    if not kit_is_source():
        entries = [r for r in entries if r["ns"] != "dev"]
    # `--help` каждого скрипта — отдельный процесс: полсотни команд по очереди дают
    # секунды ожидания на первом открытии панели, и она выглядит зависшей. Процессы ждут
    # ввода-вывода, поэтому греем кэш параллельно, а разбор идёт уже по готовому тексту.
    with ThreadPoolExecutor(max_workers=8) as pool:
        pool.map(K.help_text, [r["impl"] for r in entries
                               if r["impl"].split()[0].endswith(".py")])
    rows = []
    for r in entries:
        impl = r["impl"]
        script = impl.split()[0]
        rows.append({
            **r,
            "script": script if script.endswith(".py") else "",
            "fixed_flags": impl.split()[1:],
            "flags": K.flags_of(impl).split() if script.endswith(".py") else [],
            "flag_help": K.flag_help(impl) if script.endswith(".py") else {},
            # какие флаги требуют значения: без этого панель шлёт `--jql` голым
            "flag_args": K.flag_args(impl) if script.endswith(".py") else {},
            # без них команда не запустится: панель включает их сразу, а не после ошибки
            "flag_required": K.required_flags(impl) if script.endswith(".py") else [],
            "args": K.args_of(impl) if script.endswith(".py") else "",
            "runnable": script.endswith(".py"),
            # флаги всегда читаются из kit'а, а запускается движок проекта — кроме этих
            "from_kit": script in KIT_SIDE,
        })
    CACHE["registry"] = rows
    try:
        with open(REGISTRY_CACHE, "w", encoding="utf-8") as f:
            json.dump({"key": key, "rows": rows}, f, ensure_ascii=False)
    except OSError:
        pass        # кэш — ускорение, а не результат работы: не записался, так не записался
    return rows


def command_by_name(name: str) -> dict | None:
    return next((r for r in registry() if r["cmd"] == name), None)


# ------------------------------------------------------------------- корни поиска

# Где панель ищет проекты. Список пользовательский, а не свойство kit'а: kit кладут куда
# угодно, проекты держат где угодно, и «папка рядом с kit'ом» верна только в первый день.
# Поэтому он живёт в домашней папке — переживает и переезд kit'а, и его переустановку.
ROOTS_FILE = os.path.join(os.path.expanduser("~"), ".aurora", "cockpit-roots.txt")

# Где панели вообще разрешено разворачивать проект. Список разрешённого, а не запретного:
# перечислить все системные деревья трёх ОС нельзя, а места, где живут рабочие папки,
# наперечёт — домашняя папка, чужие домашние, примонтированные диски, временный каталог.
def allowed_bases() -> tuple:
    home = os.path.expanduser("~")
    return tuple(os.path.realpath(b) for b in
                 (home, "/Users", "/home", "/Volumes", "/mnt", "/media", "/srv",
                  tempfile.gettempdir()))


def norm(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path.strip()))


def load_roots(cli: list | None = None) -> list:
    """Корни поиска: из --roots, иначе из файла, иначе папка рядом с kit'ом.

    Значение из `--roots` не записывается: это разовый запуск «посмотреть вон те папки»,
    а не смена настройки.
    """
    if cli:
        return [norm(r) for r in cli]
    saved = []
    if os.path.isfile(ROOTS_FILE):
        saved = [norm(l) for l in open(ROOTS_FILE, encoding="utf-8").read().splitlines()
                 if l.strip() and not l.startswith("#")]
    return saved or [norm(os.path.dirname(KIT))]


def save_roots(roots: list) -> None:
    os.makedirs(os.path.dirname(ROOTS_FILE), exist_ok=True)
    uniq = list(dict.fromkeys(norm(r) for r in roots))
    with open(ROOTS_FILE, "w", encoding="utf-8") as f:
        f.write("# Где панель Авроры ищет проекты — по одному пути в строке.\n")
        f.write("\n".join(uniq) + "\n")


def writable_target(target: str) -> str:
    """→ причина отказа, либо пустая строка."""
    home = os.path.expanduser("~")
    if target in (home, os.sep):
        return "нельзя разворачивать проект прямо в домашней или корневой папке"
    real = os.path.realpath(target)
    if any(real == b or real.startswith(b + os.sep) for b in allowed_bases()):
        return ""
    return (f"{target} — за пределами домашней папки и примонтированных дисков. "
            "Проекту место рядом с вашими рабочими файлами, а не в системных деревьях.")


# ------------------------------------------------------------------- проекты

def find_projects(roots: list, depth: int = 3) -> list:
    out = []
    seen = set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".") and
                       d not in ("node_modules", "__pycache__", "Sources", "Raw",
                                 "AuroraKnowledgeDB", "Artifacts", "Deliverables",
                                 "Workspaces", "Templates", "Prompts")]
            if dirpath.count(os.sep) - base_depth >= depth:
                dirs[:] = []
            if "aurora.config.yaml" in files and dirpath not in seen:
                seen.add(dirpath)
                out.append(project_card(dirpath))
    return sorted(out, key=lambda p: p["name"].lower())


def read_text(path: str, limit: int = 400_000) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception:
        return ""


def config_value(text: str, key: str, default: str = "") -> str:
    m = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
    return m.group(1).strip() if m else default


def project_card(path: str) -> dict:
    cfg = read_text(os.path.join(path, "aurora.config.yaml"))
    ver = read_text(os.path.join(path, "AuroraKnowledgeDB", "meta", "aurora_version.txt")).strip()
    env = os.path.join(path, ".env.aurora.local")
    env_text = read_text(env)
    def filled(name):
        # \s после «=» съедает переводы строк и цепляет следующую непустую строку файла:
        # пустой токен показывался как «заполнен». Разделители ищем только внутри строки.
        m = re.search(rf"^{name}[ \t]*=[ \t]*(\S.*)$", env_text, re.M)
        return bool(m and m.group(1).strip())
    return {
        "path": path,
        "id": path,
        "name": config_value(cfg, "name") or os.path.basename(path),
        "slug": config_value(cfg, "slug"),
        "engine": ver or "—",
        "kit": kit_version(),
        "behind": ver != kit_version() and bool(ver),
        "space": config_value(cfg, "space"),
        "jira_key": config_value(cfg, "project_key"),
        "privacy": config_value(cfg, "scrub", "report"),
        "has_env": os.path.isfile(env),
        # Каким модулям есть чем авторизоваться. Имена переменных модуль объявляет
        # префиксом (CONFLUENCE_PAT, NOTION_PAT…), поэтому список читаем из самого
        # файла доступов, а не держим в панели перечень известных продуктов.
        "tokens": sorted({m.group(1) for m in re.finditer(
            r"^([A-Z][A-Z0-9_]*?)_(?:PAT|PERSONAL_TOKEN|PASSWORD)[ \t]*=[ \t]*\S", env_text, re.M)}),
        "confluence_token": filled("CONFLUENCE_PERSONAL_TOKEN") or filled("CONFLUENCE_PAT"),
        "jira_token": filled("JIRA_PERSONAL_TOKEN") or filled("JIRA_PAT"),
        "git_branch": git_branch(path),
        "dirty": git_dirty_count(path),
    }


def ui_version() -> str:
    """Версия панели объявлена в самом HTML — там же, где она используется."""
    m = re.search(r'const UI_VERSION = "([^"]+)"', read_text(UI, limit=200_000))
    return m.group(1) if m else "—"


def minor(v: str) -> str:
    return ".".join(v.split(".")[:2])


def kit_version() -> str:
    return read_text(os.path.join(KIT, "VERSION")).strip() or "—"


def git_branch(path: str) -> str:
    try:
        out = subprocess.run(["git", "-C", path, "branch", "--show-current"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip()
    except Exception:
        return ""


def git_dirty_count(path: str) -> int:
    try:
        out = subprocess.run(["git", "-C", path, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=30)
        return len([l for l in out.stdout.splitlines() if l.strip()
                    and "__pycache__" not in l])
    except Exception:
        return 0


# -------------------------------------------------------------------- здоровье

# Скрипты, которые работают ОТ kit'а: они читают манифест, схему папок и версию kit'а.
# Копия такого скрипта внутри проекта не знает, где kit, и падает на отсутствующем
# манифесте — именно так ломался «Предпросмотр обновления» в панели.
# `aurora_setup.py` тоже отсюда: у проекта лежит копия времён его установки, и режим
# формы (--json) в ней может отсутствовать — панель всегда работает с текущей версией.
KIT_SIDE = ("aurora_update.py", "install_aurora.py", "kit_commands.py", "aurora_setup.py")


def kit_is_source() -> bool:
    """Панель поднята из самого кита, а не из копии движка внутри проекта.

    Команды `dev:` разрабатывают движок и выполняются в его дереве: тест-кейсы, автотесты
    и `Development/` живут там. В проекте их нет — и показывать их аналитику незачем.
    """
    return (os.path.isfile(os.path.join(KIT, "engine_manifest.txt"))
            and not os.path.isfile(os.path.join(KIT, "aurora.config.yaml")))


def script_path(project: str, script: str) -> str:
    """Где взять скрипт: kit-сторонние — всегда из kit'а, остальные — из движка проекта."""
    if script in KIT_SIDE:
        return os.path.join(KIT, "scripts", script)
    path = os.path.join(project, ".opencode", "scripts", script)
    return path if os.path.isfile(path) else os.path.join(KIT, "scripts", script)


def run_capture(project: str, script: str, args: list, timeout: int = 300) -> tuple:
    """→ (rc, stdout+stderr)."""
    path = script_path(project, script)
    if not os.path.isfile(path):
        return 127, f"нет скрипта {script}"
    try:
        p = subprocess.run([sys.executable, path, *args], cwd=project,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"{script}: превышено время ожидания {timeout} с"
    except Exception as e:
        return 1, f"{script}: {e}"


def health(project: str) -> dict:
    rc, out = run_capture(project, "aurora_stats.py", ["--json"])
    try:
        stats = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        stats = {"error": out.strip()[:400]}

    # Полный линт вместо --summary: он стоит те же полсекунды, но заодно отдаёт разбивку
    # по видам ошибок — из неё дашборд показывает то, что человек чинит отдельными
    # командами (конфликты синонимов, двойники), а не только общее число.
    rc_l, lint = run_capture(project, "kb_lint.py", [])
    m = re.search(r"карточек (\d+), ошибок (\d+)", lint)
    lint_info = {"cards": int(m.group(1)), "errors": int(m.group(2))} if m else {"raw": lint[:300]}
    lint_info["kinds"] = {k.strip(): int(n)
                          for k, n in re.findall(r"^## (.+?):\s*(\d+)\s*$", lint, re.M)}
    baseline = read_text(os.path.join(project, "AuroraKnowledgeDB", "meta", "lint_baseline.txt")).strip()
    lint_info["baseline"] = int(baseline) if baseline.isdigit() else None

    rc_d, doc = run_capture(project, "aurora_doctor.py", [])
    doctor = {
        "rc": rc_d,
        "errors": [l[7:].strip() for l in doc.splitlines() if l.startswith("ERROR:")],
        "warns": [l[6:].strip() for l in doc.splitlines() if l.startswith("WARN:")],
        "engine": (re.search(r"^движок:\s*(\S+)", doc, re.M) or [None, "—"])[1],
        "privacy": (re.search(r"privacy\.scrub = (\w+)", doc) or [None, "report"])[1],
    }

    # Аудит отдаёт итог по каждому зеркалу сам: разбирать его текст позиционно
    # («первое MISSING — Confluence, второе — Jira») нельзя, зеркал бывает сколько угодно.
    rc_a, aud = run_capture(project, "sync_audit.py", ["--json"])
    try:
        mirrors = json.loads(aud[aud.index("{"):aud.rindex("}") + 1]).get("mirrors", {})
    except Exception:
        # движок проекта старее 1.28 и про --json не знает: читаем обычный отчёт,
        # но по заголовкам разделов, а не по порядку чисел
        rc_a, aud = run_capture(project, "sync_audit.py", [])
        mirrors = {}
        for chunk in re.split(r"^## ", aud, flags=re.M)[1:]:
            name = chunk.split("(", 1)[0].strip()
            nums = re.search(r"MISSING: \*\*(\d+)\*\*.*?ORPHAN: \*\*(\d+)\*\*", chunk, re.S)
            if name and nums:
                mirrors[name] = {"missing": int(nums.group(1)), "orphan": int(nums.group(2))}
    return {"stats": stats, "lint": lint_info, "doctor": doctor, "mirrors": mirrors,
            "build": build_progress(project), "agent": last_agent_run(project),
            "sources": sources(project), "runs": read_runlog(project)}


def build_progress(project: str) -> dict:
    """Где мы в сборке базы из источников — главное число всей работы.

    Дашборд показывал здоровье уже собранного и молчал о том, сколько осталось собрать:
    человек, который ведёт базу, узнавал это только запустив `kb:build`.
    """
    rc, out = run_capture(project, "build_plan.py", ["--status"])
    m = re.search(r"Источников:\s*(\d+)\s*·\s*обработано:\s*(\d+)\s*\((\d+) карточ\w*\)"
                  r"\s*·\s*осталось:\s*(\d+)", out)
    if not m:
        return {}
    total, done, cards, left = (int(m.group(i)) for i in range(1, 5))
    return {"total": total, "done": done, "cards": cards, "left": left,
            "pct": round(done * 100 / total, 1) if total else 0.0}


def last_agent_run(project: str) -> dict:
    """Последний прогон встроенного агента: когда, что сделал, чем кончился оракул."""
    d = os.path.join(project, "AuroraKnowledgeDB", "meta", "agent-runs")
    try:
        files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    except OSError:
        return {}
    if not files:
        return {}
    text = read_text(os.path.join(d, files[-1]), limit=200_000)
    ok = "**Оракул:** ✅" in text
    why = (re.search(r"\*\*Оракул:\*\*\s*[✅✗]\s*(.+)", text) or [None, ""])[1]
    left = (re.search(r"## Осталось на следующий прогон:\s*(\d+)", text) or [None, "0"])[1]
    return {"file": files[-1], "ok": ok, "why": why.strip(),
            "left": int(left), "task": files[-1].rsplit("_", 1)[-1][:-3]}


# ------------------------------------------------------- журнал запусков проекта

RUNLOG = os.path.join(".opencode", "run_log.md")
RUNLOG_HEAD = """# Журнал запусков

Кто и когда последний раз запускал команду Авроры в этом проекте. Файл лежит в git
рядом с движком, поэтому ответ на «когда обновляли зеркала» есть у всей команды, а не
только у того, у кого открыта вкладка панели.

Пишет панель (Cockpit) после каждого запуска — по строке на команду, последний прогон.
Запуски из терминала сюда не попадают: у них нет общей точки, через которую проходят все
команды. Код возврата: 0 — сделано, 1 — команда отработала и нашла, что чинить,
2 и выше — не отработала.

| Команда | Когда (UTC) | Код | Ядро | Кто | Строка запуска |
|---|---|---|---|---|---|
"""


def read_runlog(project: str) -> dict:
    """Журнал → {команда: запись}. Пустой файл, чужие правки и мусор — просто нет записи."""
    runs = {}
    for line in read_text(os.path.join(project, RUNLOG), limit=200_000).splitlines():
        c = [x.strip() for x in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if len(c) != 6 or not c[0] or c[0] in ("Команда", "---") or set(c[0]) == {"-"}:
            continue
        runs[c[0]] = {"at": c[1], "rc": int(c[2]) if c[2].lstrip("-").isdigit() else None,
                      "kit": c[3], "who": c[4], "line": c[5]}
    return runs


def who(project: str) -> str:
    """Имя из git этого проекта — тем же, кем подписаны коммиты рядом."""
    try:
        p = subprocess.run(["git", "config", "user.name"], cwd=project,
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except Exception:
        pass
    return getpass.getuser()


def write_runlog(project: str, cmd: str, rc: int, line: str) -> None:
    """Обновить строку команды. Порядок — по имени команды: так дифф остаётся коротким.

    Пишем последний запуск, а не всю хронологию: файл в git, и журнал, растущий на строку
    от каждого прогона, превратится в источник конфликтов при слиянии веток.
    """
    runs = read_runlog(project)
    runs[cmd] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rc": rc,
                 "kit": kit_version(), "who": who(project), "line": line}
    body = "".join(
        f"| {c} | {r['at']} | {r['rc']} | {r['kit']} | {r['who']} | {r['line']} |\n"
        for c, r in sorted(runs.items()))
    path = os.path.join(project, RUNLOG)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(RUNLOG_HEAD + body)
    except OSError:
        pass    # журнал — удобство, а не результат работы: не записался, так не записался


def sources(project: str) -> dict:
    """Что за модули источников установлены и что подключено — спрашиваем реестр проекта."""
    rc, out = run_capture(project, "sources_registry.py", ["--json"])
    try:
        return json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        return {"installed": [], "instances": [], "error": out.strip()[:300]}


def _agent_venv_ok() -> bool:
    try:
        import agent_core as AG
        return AG.venv_status()[0]
    except Exception:  # noqa: BLE001
        return False


def environment() -> dict:
    """Что установлено на машине и какие команды от этого зависят.

    Наличие модуля проверяем поиском, а не импортом: `import markitdown` тянет за собой
    половину экосистемы и занимал секунды на каждом открытии панели — а панели нужно
    знать только «есть или нет». Результат держим до перезапуска: список установленного
    за сессию не меняется, а если поставили пакет — панель перезапускают.
    """
    if "env" in CACHE:
        return CACHE["env"]

    def has_module(name):
        try:
            return importlib.util.find_spec(name) is not None
        except Exception:  # noqa: BLE001
            return False

    def has_bin(name):
        return shutil.which(name) is not None

    mcp = os.path.expanduser("~/.cursor/mcp.json")
    mcp_ok = False
    if os.path.isfile(mcp):
        try:
            d = json.loads(read_text(mcp))
            srv = (d.get("mcpServers") or {})
            mcp_ok = any("atlas" in k.lower() for k in srv)
        except Exception:
            mcp_ok = False
    out = {
        "python": sys.version.split()[0],
        "items": [
            {"name": "git", "ok": has_bin("git"), "kind": "bin",
             "enables": "всё: движок работает поверх git", "install": "brew install git"},
            {"name": "pandoc", "ok": has_bin("pandoc"), "kind": "bin",
             "enables": "ship:export — markdown → docx/pdf", "install": "brew install pandoc"},
            {"name": "beautifulsoup4", "ok": has_module("bs4"), "kind": "py",
             "enables": "sync:confluence — разбор storage-разметки",
             "install": "pip3 install beautifulsoup4"},
            {"name": "markdownify", "ok": has_module("markdownify"), "kind": "py",
             "enables": "sync:confluence — HTML → markdown", "install": "pip3 install markdownify"},
            {"name": "lxml", "ok": has_module("lxml"), "kind": "py",
             "enables": "sync:confluence — быстрый парсер", "install": "pip3 install lxml"},
            {"name": "markitdown", "ok": has_module("markitdown"), "kind": "py",
             "enables": "kb:ingest-office — docx/pptx → markdown", "install": "pip3 install markitdown"},
            {"name": "openpyxl", "ok": has_module("openpyxl"), "kind": "py",
             "enables": "kb:ingest-office — xlsx", "install": "pip3 install openpyxl"},
            {"name": "pypdf", "ok": has_module("pypdf") or has_module("fitz"), "kind": "py",
             "enables": "kb:ingest-office — pdf", "install": "pip3 install pypdf"},
            {"name": "Pydantic AI (встроенный агент)", "ok": _agent_venv_ok(), "kind": "py",
             "enables": "agent:* — адаптер по умолчанию; без него агент работает на "
                        "stdlib-фолбэке",
             "install": "кнопка «Установить / Обновить» в «Настройка» → «Агент»"},
            {"name": "Atlassian MCP в Cursor", "ok": mcp_ok, "kind": "mcp",
             "enables": "работа ассистента с Confluence/Jira из редактора",
             "install": "Cursor → Settings → MCP → mcp-atlassian"},
        ],
    }
    CACHE["env"] = out
    return out


# ----------------------------------------------------------------- встроенный агент

def agent_state(project: str) -> dict:
    """Конфигурация агента глазами панели: ключи маской, цель записи названа явно.

    Слои те же, что у самого агента: кит < проект. Панель не изобретает свой разбор —
    импортирует agent_core, чтобы форма и движок никогда не разошлись в прочтении.
    """
    import agent_core as AG
    env = dict(AG.load_env(Path(KIT) / ".env.aurora.local"))
    if project:
        env.update(AG.load_env(Path(project) / ".env.aurora.local"))
    cfg = AG.parse_config(env)
    venv_ok, venv_ver = AG.venv_status()
    target = (os.path.join(project, ".env.aurora.local") if project
              else os.path.join(KIT, ".env.aurora.local"))
    return {
        "target": target,
        "target_label": (f"проект «{os.path.basename(project)}»" if project
                         else "глобально (кит) — общая настройка всех проектов"),
        "adapter": cfg["adapter"], "thinking": cfg["thinking"],
        "max_steps": cfg["max_steps"], "budget_min": cfg["budget_min"],
        "request_timeout": cfg["request_timeout"],
        "backends": [{"n": b["n"], "url": b["url"], "key_set": bool(b["key"]),
                      "model": b["model"], "models": b["models"]} for b in cfg["backends"]],
        # Ключ наружу не отдаём никогда — только «заполнен или нет», как и у бэкендов.
        "embed": {"url": cfg["embed"]["url"], "model": cfg["embed"]["model"],
                  "key_set": bool(cfg["embed"]["key"])},
        "venv": {"ok": venv_ok, "version": venv_ver, "path": str(AG.VENV)},
    }


def agent_write_env(project: str, vars: dict) -> dict:
    """Дописать/заменить AURORA_AGENT_* в целевом .env, не трогая остальные строки.

    Пустое значение удаляет переменную. Ключи вне AURORA_AGENT_ не принимаются: эта
    ручка настраивает агента, а не редактирует произвольные секреты.
    """
    # AURORA_EMBED_* — тот же контур агента: свой сервис векторов у него бывает
    # отдельным (свой адрес, свой ключ, своя модель), но настраивается он здесь же.
    bad = [k for k in vars if not k.startswith(("AURORA_AGENT_", "AURORA_EMBED_"))]
    if bad:
        return {"error": "не агентские переменные: " + ", ".join(bad[:3])}
    target = Path(project or KIT) / ".env.aurora.local"
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    for key, value in vars.items():
        value = (value or "").strip()
        hit = next((i for i, l in enumerate(lines)
                    if l.split("=")[0].strip() == key), None)
        if value:
            if hit is None:
                lines.append(f"{key}={value}")
            else:
                lines[hit] = f"{key}={value}"
        elif hit is not None:
            del lines[hit]
    target.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return {"ok": True, "target": str(target), "written": len(vars)}


def agent_ping(project: str) -> dict:
    """Живой прогон цепочки. Подпроцессом и с cwd проекта: наслоение .env — как у агента."""
    script = script_path(project or KIT, "agent_core.py")
    try:
        p = subprocess.run([sys.executable, script, "--ping", "--json"],
                           cwd=project or KIT, capture_output=True, text=True, timeout=180)
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001
        return {"error": f"ping не выполнен: {type(e).__name__}: {e}"}


def agent_venv_install() -> dict:
    """Поставить/обновить Pydantic AI. Синхронно: локальная панель, пользователь ждёт."""
    try:
        p = subprocess.run([sys.executable, os.path.join(KIT, "scripts", "agent_core.py"),
                            "--venv-install"], capture_output=True, text=True, timeout=900)
        CACHE.pop("env", None)      # строка в «Установке» обязана обновиться
        return {"ok": p.returncode == 0, "log": (p.stdout + p.stderr).strip()[-600:]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"установка не выполнена: {type(e).__name__}: {e}"}


# ----------------------------------------------------------------- выполнение

def start_job(project: str, cmd: str, extra: list) -> str:
    row = command_by_name(cmd)
    if not row or not row["runnable"]:
        raise ValueError(f"команда «{cmd}» не запускается панелью")
    allowed = set(row["flags"]) | {"--apply", "--allow-dirty", "--force", "--json"}
    args = list(row["fixed_flags"])
    for a in extra:
        head = a.split("=")[0]
        if head.startswith("--") and head not in allowed:
            raise ValueError(f"флаг {head} не объявлен командой «{cmd}»")
        args.append(a)

    path = script_path(project, row["script"])
    job_id = secrets.token_hex(8)
    job = {"id": job_id, "cmd": cmd, "args": args, "project": project, "rc": None,
           "out": [], "started": time.time(), "done": False}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def worker():
        try:
            # Python буферизует stdout, когда на том конце не терминал: длинная команда
            # (синк на семьсот страниц, прогон агента) молчала минутами, а потом
            # вываливала всё разом. Человек в это время не знает, работает она или висит.
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            p = subprocess.Popen([sys.executable, path, *args], cwd=project, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1)
            for line in p.stdout:
                with JOBS_LOCK:
                    job["out"].append(line.rstrip("\n"))
                    if len(job["out"]) > 4000:
                        job["out"] = job["out"][-4000:]
            p.wait()
            job["rc"] = p.returncode
        except Exception as e:
            job["out"].append(f"cockpit: {e}")
            job["rc"] = 2       # команда не отработала вовсе — это не «нашла, что чинить»
        finally:
            job["done"] = True
            job["finished"] = time.time()
            write_runlog(project, cmd, job["rc"], (cmd + " " + " ".join(args)).strip())

    threading.Thread(target=worker, daemon=True).start()
    return job_id


# --------------------------------------------------------------------- сервер

class Handler(BaseHTTPRequestHandler):
    server_version = "AuroraCockpit"

    def log_message(self, fmt, *args):
        pass

    # --- защита: только localhost, только со своим токеном
    def guarded(self, query: dict) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self.send_json({"error": "панель отвечает только на 127.0.0.1"}, 403)
            return False
        tok = (query.get("t", [""])[0] or self.headers.get("X-Aurora-Token", ""))
        if not secrets.compare_digest(tok, TOKEN):
            self.send_json({"error": "нет токена сессии — откройте адрес из консоли"}, 403)
            return False
        return True

    def send_json(self, payload, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            html = read_text(UI, limit=4_000_000)
            if not html:
                html = "<h1>cockpit/ui/index.html не найден</h1>"
            html = html.replace("__AURORA_TOKEN__", TOKEN)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.guarded(q):
            return
        if u.path == "/api/ping":
            # по этому ответу второй запуск узнаёт свою же панель, а не чужую программу
            self.send_json({"app": "aurora-cockpit", "kit": kit_version(),
                            "pid": os.getpid()})
        elif u.path == "/api/state":
            self.send_json({
                "kit": {"version": kit_version(), "path": KIT},
                "ui": {"version": ui_version(),
                       "behind": minor(ui_version()) != minor(kit_version())},
                "projects": find_projects(self.server.roots),
                "env": environment(),
                "commands": registry(),
                # пасхалка «Разработка» открывается только там, где есть что разрабатывать
                "dev_available": kit_is_source(),
            })
        elif u.path == "/api/health":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json(health(project))
        elif u.path == "/api/config":
            project = q.get("project", [""])[0]
            if not self._known(project):
                return
            self.send_json({"text": read_text(os.path.join(project, "aurora.config.yaml")),
                            "path": "aurora.config.yaml"})
        elif u.path == "/api/roots":
            self.send_json({"roots": [norm(r) for r in self.server.roots],
                            "file": ROOTS_FILE})
        elif u.path == "/api/agent":
            self.send_json(agent_state(q.get("project", [""])[0]))
        elif u.path == "/api/about":
            self.send_json(about())
        elif u.path == "/api/scenarios":
            self.send_json({"scenarios": scenarios()})
        elif u.path == "/api/skins":
            self.send_json({"skins": skins()})
        elif u.path == "/api/skin":
            css = skin_css(q.get("id", [""])[0])
            body = css.encode("utf-8")
            self.send_response(200 if css else 404)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/kit/status":
            self.send_json(kit_git_status())
        elif u.path == "/api/doc":
            rel = os.path.normpath(q.get("path", [""])[0]).lstrip("/")
            if not any(rel == r or rel.startswith(r.rstrip("/") + "/") for r in DOC_ROOTS):
                self.send_json({"error": "этот файл панель не показывает"}, 403)
                return
            full = os.path.join(KIT, rel)
            if not os.path.isfile(full):
                self.send_json({"error": f"нет файла {rel}"}, 404)
                return
            self.send_json({"path": rel, "text": read_text(full)})
        elif u.path == "/api/job":
            job = JOBS.get(q.get("id", [""])[0])
            if not job:
                self.send_json({"error": "задание не найдено"}, 404)
                return
            since = int(q.get("since", ["0"])[0])
            with JOBS_LOCK:
                lines = job["out"][since:]
                self.send_json({"id": job["id"], "lines": lines, "next": since + len(lines),
                                "done": job["done"], "rc": job["rc"], "cmd": job["cmd"],
                                "args": job["args"]})
        else:
            self.send_json({"error": "неизвестный маршрут"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self.guarded(q):
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json({"error": "тело запроса не разобрано"}, 400)
            return
        if u.path == "/api/config":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_config(project, payload.get("text", "")))
            return
        if u.path == "/api/kit/update":
            self.send_json(kit_pull())
            return
        if u.path == "/api/confluence/resolve":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._resolve_refs(project, payload.get("refs") or []))
            return
        if u.path == "/api/setup":
            project = payload.pop("project", "")
            if not self._known(project):
                return
            self.send_json(self._run_setup(project, payload))
            return
        if u.path == "/api/sources":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_sources(project, payload.get("modules") or []))
            return
        if u.path == "/api/tokens":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_tokens(project, payload))
            return
        if u.path == "/api/project/new":
            self.send_json(self._create_project(payload))
            return
        if u.path == "/api/roots":
            self.send_json(self._edit_roots(payload))
            return
        if u.path == "/api/agent/env":
            project = payload.get("project", "")
            if project and not self._known(project):
                return
            self.send_json(agent_write_env(project, payload.get("vars") or {}))
            return
        if u.path == "/api/agent/ping":
            project = payload.get("project", "")
            if project and not self._known(project):
                return
            self.send_json(agent_ping(project))
            return
        if u.path == "/api/agent/venv":
            self.send_json(agent_venv_install())
            return
        if u.path == "/api/run":
            project = payload.get("project", "")
            row = command_by_name(payload.get("cmd", ""))
            # Движковые команды выполняются в дереве кита: `dev:` целиком и `kit:skills`,
            # которая кладёт скиллы в общий каталог агента, а не в проект.
            if row and (row.get("ns") == "dev" or row.get("cmd") == "kit:skills"):
                # Контур разработки живёт в ките: и автотесты, и Development/QA лежат там,
                # а выбранный на Мостике проект к этому отношения не имеет.
                if not kit_is_source():
                    self.send_json({"error": "панель поднята не из кита — "
                                             "разрабатывать движок отсюда нечем"}, 400)
                    return
                project = KIT
            elif not self._known(project):
                return
            try:
                job_id = start_job(project, payload.get("cmd", ""),
                                   [str(x) for x in payload.get("args", [])])
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            self.send_json({"job": job_id})
        else:
            self.send_json({"error": "неизвестный маршрут"}, 404)

    def _write_config(self, project: str, text: str) -> dict:
        """Конфиг правится как текст: список корней Confluence проще дописать руками,
        чем прокликать формой. Прежняя версия сохраняется рядом — откатиться можно."""
        path = os.path.join(project, "aurora.config.yaml")
        if len(text) > 200_000:
            return {"error": "слишком большой файл"}
        if "project:" not in text:
            return {"error": "в тексте нет блока project: — это не похоже на конфиг Авроры"}
        try:
            if os.path.isfile(path):
                backup = path + ".bak"
                with open(backup, "w", encoding="utf-8") as f:
                    f.write(read_text(path))
            with open(path, "w", encoding="utf-8") as f:
                f.write(text if text.endswith("\n") else text + "\n")
        except Exception as e:
            return {"error": f"не удалось записать: {e}"}
        return {"ok": True, "backup": "aurora.config.yaml.bak"}

    def _write_tokens(self, project: str, payload: dict) -> dict:
        """Токены синка. Значение приходит от человека и НИКОГДА не отдаётся обратно:
        панель знает только «заполнено» или «пусто». Файл закрыт правами 600."""
        path = os.path.join(project, ".env.aurora.local")
        keys = ("CONFLUENCE_PERSONAL_TOKEN", "JIRA_PERSONAL_TOKEN")
        lines = read_text(path).splitlines() if os.path.isfile(path) else []
        base = read_text(os.path.join(KIT, "aurora.env.local.example")).splitlines() \
            if not lines else lines
        out, seen = [], set()
        for line in base:
            k = line.split("=", 1)[0].strip().lstrip("# ")
            if k in keys and k in payload:
                value = str(payload[k]).strip()
                if value:
                    out.append(f"{k}={value}")
                    seen.add(k)
                    continue
                if not line.startswith("#"):     # пустое значение — оставляем как было
                    out.append(line)
                    seen.add(k)
                    continue
            out.append(line)
        for k in keys:
            if k in payload and str(payload[k]).strip() and k not in seen:
                out.append(f"{k}={str(payload[k]).strip()}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(out) + "\n")
            os.chmod(path, 0o600)
        except Exception as e:
            return {"error": f"не удалось записать: {e}"}
        return {"ok": True}

    def _write_sources(self, project: str, modules: list) -> dict:
        """Переписать секцию `sources:` конфига — подключение и отключение модулей.

        Отключение не трогает саму папку зеркала: выгрузка — это данные, а данные
        панель не удаляет. После отключения doctor назовёт папку ничьей — это и есть
        приглашение решить её судьбу руками.
        """
        known = {m["id"]: m for m in sources(project).get("installed", [])}
        bad = [m for m in modules if m not in known]
        if bad:
            return {"error": "не установлены модули: " + ", ".join(bad)}
        cfg = os.path.join(project, "aurora.config.yaml")
        text = read_text(cfg)
        if not text:
            return {"error": "нет aurora.config.yaml"}
        block = ["# Подключённые модули источников: id — он же имя папки в Sources/.",
                 "# Что установлено: `python3 .opencode/scripts/sources_registry.py`.",
                 "sources:"]
        for mid in modules:
            path = known[mid]["mirror"]["default_path"].rstrip("/")
            block.append(f"  - id: {os.path.basename(path)}\n"
                         f"    module: {mid}\n    path: {path}")
        body = "\n".join(block) + "\n"
        if re.search(r"^sources:\s*$", text, re.M):
            new = re.sub(r"(^#[^\n]*\n)*^sources:\s*$.*?(?=^\S|\Z)", body, text,
                         count=1, flags=re.M | re.S)
        else:
            new = re.sub(r"^atlassian:", body + "\natlassian:", text, count=1, flags=re.M)
            if new == text:
                new = text.rstrip("\n") + "\n\n" + body
        try:
            with open(cfg, "w", encoding="utf-8") as f:
                f.write(new)
        except OSError as e:
            return {"error": f"конфиг не записан: {e}"}
        return {"ok": True, "modules": modules}

    def _resolve_refs(self, project: str, refs: list) -> dict:
        """Ссылки вида …/display/ПРОСТРАНСТВО/Заголовок → номер страницы.

        В человекочитаемом адресе номера нет — его знает только сервер. Спрашиваем его
        токеном проекта; без токена или без сети честно говорим, чего не хватило, вместо
        того чтобы записать в конфиг неработающий корень.
        """
        import confluence_export as C
        cwd = os.getcwd()
        try:
            os.chdir(project)
            cfg = C.read_config()
            auth, _kind = C.read_secret()
        finally:
            os.chdir(cwd)
        out = []
        api = C.Api(cfg["base_url"], auth) if (auth and cfg.get("base_url")) else None
        for raw in refs:
            pid, space, title = C.parse_ref(str(raw))
            if pid:
                out.append({"raw": raw, "page_id": pid, "title": ""})
                continue
            if api is None:
                out.append({"raw": raw, "error":
                            "нужен токен Confluence и адрес в конфиге, чтобы спросить "
                            "номер страницы по ссылке"})
                continue
            pid, title, err = C.resolve_ref(api, str(raw), cfg.get("space", ""))
            out.append({"raw": raw, "page_id": pid, "title": title, "error": err})
        return {"refs": out}

    def _run_setup(self, project: str, answers: dict) -> dict:
        """Настройка формой. Записывает не панель, а сам `aurora_setup.py`:
        один способ собрать конфиг, а не два расходящихся."""
        path = script_path(project, "aurora_setup.py")
        try:
            p = subprocess.run([sys.executable, path, "--target", project, "--json", "-"],
                               input=json.dumps(answers, ensure_ascii=False),
                               capture_output=True, text=True, timeout=120)
        except Exception as e:
            return {"error": str(e)}
        if p.returncode != 0:
            return {"error": (p.stderr or p.stdout)[-500:]}
        return {"ok": True, "log": (p.stdout or "").splitlines()[-8:]}

    def _edit_roots(self, payload: dict) -> dict:
        """Добавить или убрать корень поиска. Список сохраняется между запусками."""
        add, drop = (payload.get("add") or "").strip(), (payload.get("drop") or "").strip()
        roots = [norm(r) for r in self.server.roots]
        if add:
            target = norm(add)
            if not os.path.isdir(target):
                return {"error": f"нет такой папки: {target}"}
            if target not in roots:
                roots.append(target)
        if drop:
            roots = [r for r in roots if r != norm(drop)]
        if not roots:
            return {"error": "хотя бы один корень нужен — иначе панель не найдёт проекты"}
        save_roots(roots)
        self.server.roots = roots
        return {"ok": True, "roots": roots}

    def _create_project(self, payload: dict) -> dict:
        """Развернуть Аврору в новую папку — из панели, без терминала.

        Папка проекта выбирается человеком и лежать может где угодно: kit и проекты не
        обязаны быть соседями. Если путь вне корней поиска — панель не отказывает, а
        добавляет его родителя в корни, иначе только что созданный проект сам же и
        пропал бы из списка. Не разрешены только системные деревья.
        """
        raw = (payload.get("path") or "").strip()
        name = (payload.get("name") or "").strip()
        if not raw or not name:
            return {"error": "нужны путь и название проекта"}
        target = norm(raw)
        bad = writable_target(target)
        if bad:
            return {"error": bad}
        roots = [norm(r) for r in self.server.roots]
        added = ""
        if not any(target == r or target.startswith(r + os.sep) for r in roots):
            added = os.path.dirname(target) or target
            roots.append(added)
            save_roots(roots)
            self.server.roots = roots
        if os.path.isfile(os.path.join(target, "aurora.config.yaml")):
            return {"error": "здесь уже есть проект Авроры"}
        args = [os.path.join(KIT, "scripts", "install_aurora.py"),
                "--target", target, "--name", name]
        for flag, key in (("--slug", "slug"), ("--jira-key", "jira"),
                          ("--confluence-space", "space")):
            if (payload.get(key) or "").strip():
                args += [flag, payload[key].strip()]
        try:
            os.makedirs(target, exist_ok=True)
            p = subprocess.run([sys.executable, *args], capture_output=True,
                               text=True, timeout=300)
        except Exception as e:
            return {"error": str(e)}
        if p.returncode != 0:
            return {"error": (p.stderr or p.stdout)[-500:]}
        out = {"ok": True, "path": target, "log": (p.stdout or "").splitlines()[-12:]}
        if added:
            out["added_root"] = added
        return out

    def _known(self, project: str) -> bool:
        """Путь — только из списка обнаруженных проектов, не произвольная строка."""
        known = {p["path"] for p in find_projects(self.server.roots)}
        if project in known:
            return True
        self.send_json({"error": "проект не найден среди обнаруженных"}, 400)
        return False


SESSION = os.path.join(KIT, "cockpit", ".session.json")


def write_session(port: int, url: str) -> None:
    """Куда стучаться, если панель уже работает.

    Адрес одноразовый и содержит токен, поэтому второй процесс сам его не придумает:
    без этого файла «открой уже запущенную панель» невозможно — только убить и поднять
    заново, потеряв то, что человек в ней открыл.
    """
    try:
        with open(SESSION, "w", encoding="utf-8") as f:
            json.dump({"port": port, "url": url, "pid": os.getpid(),
                       "kit": kit_version()}, f)
        os.chmod(SESSION, 0o600)
    except OSError:
        pass


def read_session() -> dict:
    try:
        with open(SESSION, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def alive(url: str) -> bool:
    """Отвечает ли по этому адресу именно панель, а не чужая программа на том же порту."""
    try:
        with urllib.request.urlopen(url.replace("/?t=", "/api/ping?t="), timeout=2) as r:
            return json.load(r).get("app") == "aurora-cockpit"
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    # Адрес панели одноразовый: без токена её не открыть. Когда вывод перенаправлен —
    # запуск из IDE, из скрипта, из ассистента — буфер stdout держит адрес у себя, и
    # человек видит молчащую команду вместо ссылки.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Панель управления Aurora")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--roots", nargs="*", default=None,
                    help="где искать проекты на этот запуск; без него — сохранённый список "
                         f"({ROOTS_FILE}), а при первом старте папка рядом с kit'ом")
    ap.add_argument("--add-root", metavar="PATH", action="append",
                    help="добавить папку в сохранённый список поиска и запуститься")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--restart", action="store_true",
                    help="остановить уже работающую панель и поднять заново")
    a = ap.parse_args()

    prev = read_session()
    if a.restart and prev.get("pid") and alive(prev.get("url", "")):
        try:
            os.kill(prev["pid"], signal.SIGTERM)
            for _ in range(20):
                if not alive(prev["url"]):
                    break
                time.sleep(0.25)
            print(f"Прежняя панель остановлена (pid {prev['pid']}).")
        except OSError as e:
            print(f"Не удалось остановить прежнюю панель: {e}", file=sys.stderr)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        if e.errno not in (48, 98, 10048):        # EADDRINUSE на macOS, Linux, Windows
            raise
        url = prev.get("url", "")
        if url and alive(url):
            print(f"Панель уже работает на порту {prev.get('port', a.port)} — открываю её.")
            print(f"\n  {url}\n")
            print("Перезапустить (например, после обновления kit): "
                  "aurora.py cockpit --restart")
            if not a.no_browser:
                webbrowser.open(url)
            return 0
        print(f"Порт {a.port} занят другой программой.\n"
              f"  Свободный порт:  aurora.py cockpit --port {a.port + 1}\n"
              f"  Или перезапуск:  aurora.py cockpit --restart", file=sys.stderr)
        return 1
    roots = load_roots(a.roots)
    # Лаунчер проекта зовёт панель со своей папкой: она должна пополнять список, а не
    # подменять его — иначе панель, запущенная из проекта, перестаёт видеть остальные.
    for extra in (a.add_root or []):
        extra = norm(extra)
        if os.path.isdir(extra) and extra not in roots:
            roots.append(extra)
            if not a.roots:
                save_roots(roots)
    srv.roots = roots
    url = f"http://127.0.0.1:{a.port}/?t={TOKEN}"
    print(f"Aurora Cockpit · kit {kit_version()}")
    print(f"Проекты ищу в: {', '.join(srv.roots)}")
    print(f"\n  {url}\n")
    print("Адрес одноразовый: токен живёт в памяти процесса, при перезапуске меняется.")
    print("Остановить — Ctrl+C.")
    write_session(a.port, url)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\nОстановлено.")
    finally:
        if read_session().get("pid") == os.getpid():
            try:
                os.remove(SESSION)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
