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
import json
import os
import re
import secrets
import signal
import subprocess
import sys
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

# Документы, которые панель имеет право показать. Всё остальное читать нельзя:
# сервер живёт в репозитории с рабочими данными.
DOC_ROOTS = ("docs", "skills/aurora-vault", "CHANGELOG.md", "commands.txt", "README.md")


# ------------------------------------------------------------------- скины

SKINS_DIR = os.path.join(KIT, "cockpit", "skins")


def skins() -> list:
    """Оформление вынесено в файлы: положил свой .css в cockpit/skins/ — он в списке.

    Имя и описание берутся из шапки самого файла (`/* name: … about: … */`), чтобы
    добавление скина не требовало править ни сервер, ни панель.
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
        out.append({"id": f[:-4], "name": name, "about": about})
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
            cur = {"id": m.group(1), "title": m.group(2).strip(),
                   "when": (m.group(3) or "").strip(), "steps": []}
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
    import kit_commands as K
    rows = []
    for r in K.read_registry():
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
            "args": K.args_of(impl) if script.endswith(".py") else "",
            "runnable": script.endswith(".py"),
            # флаги всегда читаются из kit'а, а запускается движок проекта — кроме этих
            "from_kit": script in KIT_SIDE,
        })
    CACHE["registry"] = rows
    return rows


def command_by_name(name: str) -> dict | None:
    return next((r for r in registry() if r["cmd"] == name), None)


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

    rc_l, lint = run_capture(project, "kb_lint.py", ["--summary"])
    m = re.search(r"карточек (\d+), ошибок (\d+)", lint)
    lint_info = {"cards": int(m.group(1)), "errors": int(m.group(2))} if m else {"raw": lint[:300]}
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

    rc_a, aud = run_capture(project, "sync_audit.py", [])
    nums = re.findall(r"MISSING: \*\*(\d+)\*\*.*?ORPHAN: \*\*(\d+)\*\*", aud)
    mirrors = {
        "confluence": {"missing": int(nums[0][0]), "orphan": int(nums[0][1])} if len(nums) > 0 else None,
        "jira": {"missing": int(nums[1][0]), "orphan": int(nums[1][1])} if len(nums) > 1 else None,
        "stale": bool(re.search(r"(\d+) дн\. назад", aud)),
    }
    return {"stats": stats, "lint": lint_info, "doctor": doctor, "mirrors": mirrors}


def environment() -> dict:
    """Что установлено на машине и какие команды от этого зависят."""
    def has_module(name):
        try:
            __import__(name)
            return True
        except Exception:
            return False

    def has_bin(name):
        return subprocess.run(["which", name], capture_output=True).returncode == 0

    mcp = os.path.expanduser("~/.cursor/mcp.json")
    mcp_ok = False
    if os.path.isfile(mcp):
        try:
            d = json.loads(read_text(mcp))
            srv = (d.get("mcpServers") or {})
            mcp_ok = any("atlas" in k.lower() for k in srv)
        except Exception:
            mcp_ok = False
    return {
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
            {"name": "Atlassian MCP в Cursor", "ok": mcp_ok, "kind": "mcp",
             "enables": "работа ассистента с Confluence/Jira из редактора",
             "install": "Cursor → Settings → MCP → mcp-atlassian"},
        ],
    }


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
            p = subprocess.Popen([sys.executable, path, *args], cwd=project,
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
            job["rc"] = 1
        finally:
            job["done"] = True
            job["finished"] = time.time()

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
            self.send_json({"roots": [os.path.abspath(os.path.expanduser(r))
                                      for r in self.server.roots]})
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
        if u.path == "/api/tokens":
            project = payload.get("project", "")
            if not self._known(project):
                return
            self.send_json(self._write_tokens(project, payload))
            return
        if u.path == "/api/project/new":
            self.send_json(self._create_project(payload))
            return
        if u.path == "/api/run":
            project = payload.get("project", "")
            if not self._known(project):
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

    def _create_project(self, payload: dict) -> dict:
        """Развернуть Аврору в новую папку — из панели, без терминала.

        Путь разрешён только внутри корней, по которым панель и так ищет проекты:
        произвольное место на диске из браузера не создаётся.
        """
        raw = (payload.get("path") or "").strip()
        name = (payload.get("name") or "").strip()
        if not raw or not name:
            return {"error": "нужны путь и название проекта"}
        target = os.path.abspath(os.path.expanduser(raw))
        roots = [os.path.abspath(os.path.expanduser(r)) for r in self.server.roots]
        if not any(target == r or target.startswith(r + os.sep) for r in roots):
            return {"error": "путь вне корней поиска панели: " + ", ".join(roots)}
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
        return {"ok": True, "path": target, "log": (p.stdout or "").splitlines()[-12:]}

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
    ap = argparse.ArgumentParser(description="Панель управления Aurora")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--roots", nargs="*", default=[os.path.dirname(KIT)],
                    help="где искать проекты (по умолчанию — папка рядом с kit'ом)")
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
    srv.roots = a.roots
    url = f"http://127.0.0.1:{a.port}/?t={TOKEN}"
    print(f"Aurora Cockpit · kit {kit_version()}")
    print(f"Проекты ищу в: {', '.join(a.roots)}")
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
