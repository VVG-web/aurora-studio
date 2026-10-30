#!/usr/bin/env python3
"""code_sync.py — клоны репозиториев кода в `Code/`: клон, обновление, закреплённая ревизия; состояние, языки, размер.

Состав репозиториев — реестр `code_repos:` в aurora.config.yaml (раздел панели «Код»). Клон — только для чтения:
команда ничего не сливает и не отправляет. Она забирает свежее с сервера (`fetch`) и ставит рабочее дерево на
нужную ревизию без ветки (`checkout --detach`): на закреплённую (`pin` — SHA или тег) или на вершину ветки сервера.
Переписанная на сервере история — не ошибка, просто новая цель. Клон с правками или клон чужого адреса команда не
трогает и говорит почему; «Сбросить клон» (`--reset`) возвращает его к реестру — правки в клоне не живут, код правят в
его репозитории.

Вход — как у «Git проекта» (`git_sync`): секрет вне проекта, git получает его из окружения через помощника учётных
данных, в адрес и командную строку он не попадает. Сеть — под проверкой соединений: репозиторий с режимом доступа
(`access`: `internet`, `vpn:<профиль>`), чья связь сейчас недоступна, пропускается с точной причиной, остальные идут.

Языки и размер (решение владельца 30.10.2026: стек неизвестен — движок определяет сам) считаются по файлам клона без
внешних инструментов: язык — по расширению и имени, строки — по переводам строк. Чужие зависимости, сгенерированное,
минифицированное, двоичное, файлы блокировок и секретные имена в счёт не идут, а называются отдельно.

Без `--apply` команда ничего не пишет и в сеть не ходит: печатает план. Коды возврата: 0 — всё синхронизировано
(или реестр пуст), 1 — часть репозиториев не синхронизирована (причина у каждого), 2 — сбой выполнения или отказ
проверки соединений (строка `AURORA-NET`).

  python3 .aurora/scripts/code_sync.py --sync                      # план: что склонируется, куда встанет
  python3 .aurora/scripts/code_sync.py --sync --apply [--repo ИМЯ]   # клонировать и обновить
  python3 .aurora/scripts/code_sync.py --sync --reset --repo ИМЯ --apply   # сбросить клон к реестру и обновить
  python3 .aurora/scripts/code_sync.py --status [--json]             # ревизия, отставание, языки и размер

Панель: `code:sync`, `code:status`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
for _s in (sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на «—»; движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_repos as CREPO  # noqa: E402
import git_sync as GS  # noqa: E402
from aurora_common import CODE_DIR, engine_dir, lock_release, lock_take, utc_stamp  # noqa: E402

NET_TIMEOUT = 3600                 # клон большого репозитория — десятки минут; маршрут ждёт без предела, git — час
CLONE_CFG = ["--config", "core.autocrlf=false", "--config", "core.longpaths=true"]
NO_LFS = {"GIT_LFS_SKIP_SMUDGE": "1"}   # двоичное LFS не тянем: его не разбирают и не читают
MAX_FILE_KB = 512                  # больше — в счёт строк не идёт (`too_big`): выгрузка, а не код
WIN_PATH = 259                     # предел пути Windows без длинных путей

# Свои проблемы клона — человеческими словами (сеть, вход, права, ветка — у `git_sync.PROBLEMS`).
PROBLEMS = {
    "code_dirty_clone": ("В клоне есть правки", "Клон только для чтения: правки в нём сотрёт синхронизация. "
                         "Сбросьте клон кнопкой «Сбросить клон» (code:sync --reset --repo <имя> --apply); "
                         "код правят в его репозитории."),
    "code_foreign_clone": ("Папка клона не от этого репозитория", "В Code/<имя> лежит не клон адреса из реестра. "
                           "Если репозиторий переехал — «Сбросить клон» перенаправит клон на адрес из реестра; "
                           "если папка чужая — перенесите её в Workspaces/."),
    "code_pin_absent": ("Закреплённой ревизии нет на сервере", "Проверьте SHA или тег в поле «Ревизия» "
                        "у репозитория в разделе «Код» или очистите поле — клон пойдёт за веткой."),
    "code_empty_remote": ("Репозиторий на сервере пуст", "Клон заведён; ставить его не на что, пока в "
                          "репозитории нет ни одного коммита."),
}

LANGS = {
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy", ".gradle": "Groovy",
    ".py": "Python", ".pyi": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
    ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".vue": "Vue", ".svelte": "Svelte",
    ".go": "Go", ".cs": "C#", ".fs": "F#", ".vb": "Visual Basic", ".rb": "Ruby", ".php": "PHP", ".rs": "Rust",
    ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++",
    ".cxx": "C++", ".hpp": "C++", ".dart": "Dart", ".lua": "Lua", ".r": "R", ".pl": "Perl", ".ex": "Elixir",
    ".exs": "Elixir", ".erl": "Erlang", ".clj": "Clojure", ".sql": "SQL", ".pls": "PL/SQL", ".pkb": "PL/SQL",
    ".pks": "PL/SQL", ".plsql": "PL/SQL", ".xml": "XML", ".xsd": "XML", ".wsdl": "XML", ".yml": "YAML",
    ".yaml": "YAML", ".json": "JSON", ".toml": "TOML", ".properties": "Properties", ".ini": "INI", ".md": "Markdown",
    ".rst": "reStructuredText", ".html": "HTML", ".htm": "HTML", ".css": "CSS", ".scss": "SCSS", ".less": "Less",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".ps1": "PowerShell", ".bat": "Batch", ".cmd": "Batch",
    ".proto": "Protocol Buffers", ".graphql": "GraphQL", ".gql": "GraphQL", ".tf": "HCL", ".hcl": "HCL",
    ".feature": "Gherkin", ".bpmn": "BPMN", ".dmn": "DMN",
}
NAMES = {"dockerfile": "Dockerfile", "makefile": "Makefile", "jenkinsfile": "Groovy", "gemfile": "Ruby",
         "rakefile": "Ruby", "vagrantfile": "Ruby"}
DEPS_DIRS = {"node_modules", "vendor", ".venv", "venv", "site-packages", "pods", "bin", "obj", "target", "build",
             "dist", "out", ".gradle", ".idea", "__pycache__", "bower_components"}
LOCK_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "gemfile.lock",
              "composer.lock", "go.sum", "pipfile.lock", "npm-shrinkwrap.json"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".jar", ".war", ".ear",
              ".class", ".so", ".dll", ".exe", ".dylib", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".docx", ".xlsx",
              ".pptx", ".doc", ".xls", ".ppt", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".bin", ".mp3", ".mp4",
              ".mov", ".avi", ".psd", ".sqlite", ".db", ".pyc"}
GENERATED_NAME = re.compile(r"(_pb2(_grpc)?\.py|\.pb\.go|\.g\.dart|\.designer\.cs|\.generated\.\w+)$", re.I)
GENERATED_HEAD = re.compile(rb"@generated|Code generated .{0,80}DO NOT EDIT|<auto-generated|DO NOT EDIT", re.I)
SKIP_REASONS = ("deps", "generated", "minified", "lock", "binary", "secret", "too_big", "excluded")


# ---------------------------------------------------------------- мелочи

def _say(*a) -> None:
    print(*a, flush=True)


def problem(code: str, raw: str = "") -> dict:
    """Проблема словами: свои коды клона здесь, общие (сеть, вход, ветка) — у `git_sync`."""
    if code in PROBLEMS:
        title, fix = PROBLEMS[code]
        return {"code": code, "title": title, "fix": fix, "raw": GS.redact(raw or "")[-2500:]}
    p = GS.problem(code, raw)
    return {"code": p["code"], "title": p["title"], "fix": p["fix"], "raw": p["raw"]}


def state_file(project) -> Path:
    return Path(project) / engine_dir(project) / "state" / "code" / "sync.json"


def lock_file(project) -> Path:
    return Path(project) / engine_dir(project) / "state" / "code.lock"


def read_state(project) -> dict:
    try:
        data = json.loads(state_file(project).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(project, data: dict) -> None:
    import aurora_fs
    aurora_fs.write_json(str(state_file(project)), data)


def _git(path, args: list, rec: dict | None = None, creds: dict | None = None, network: bool = False,
         timeout: int = 120) -> tuple:
    """git в папке клона без оболочки; вход — только окружением и только для сетевых действий."""
    s = CREPO.server_of(rec) if rec is not None else None
    return GS.run_git(path, args, s, creds, timeout=timeout, network=network, extra_env=NO_LFS)


def _ok(path, *args) -> str:
    rc, out, _e = _git(path, list(args))
    return out if rc == 0 else ""


def _is_clone(target: Path) -> bool:
    """Папка — корень своего репозитория git, а не подпапка проекта (у проекта тоже есть `.git`)."""
    if not (target / ".git").exists():
        return False
    top = _ok(target, "rev-parse", "--show-toplevel")
    return bool(top) and os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(target))


def clone_state(project, name: str, rec: dict) -> dict:
    """Что лежит в `Code/<имя>` — без сети: {state: missing|foreign|dirty|ok, rev, url, dirty: [пути]}."""
    target = CREPO.clone_path(project, name)
    if not target.exists() or (target.is_dir() and not any(target.iterdir())):
        return {"state": "missing"}
    if not target.is_dir() or not _is_clone(target):
        return {"state": "foreign", "why": "not_git"}
    url = GS.strip_userinfo(_ok(target, "remote", "get-url", "origin"))
    if not url or not _same_url(url, rec.get("url") or ""):
        return {"state": "foreign", "why": "other_url", "url": url}
    rev = _ok(target, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    # правленые (в индексе и в рабочем дереве) и новые файлы; игнорируемые клоном — не правки
    dirty = [ln for ln in (_ok(target, "diff", "--name-only", "HEAD").splitlines() if rev else [])
             + _ok(target, "ls-files", "--others", "--exclude-standard").splitlines() if ln.strip()]
    return {"state": "dirty" if dirty else "ok", "rev": rev, "url": url, "dirty": dirty[:20], "dirty_count": len(dirty)}


def _same_url(a: str, b: str) -> bool:
    norm = lambda u: re.sub(r"\.git$", "", u.strip().rstrip("/"), flags=re.I).lower()   # noqa: E731
    return norm(a) == norm(b)


# ---------------------------------------------------------------- языки и размер

def _mask_rx(mask: str):
    """Маска пути в регулярку: `**` — любые папки, `*` и `?` — внутри одного имени. Маска без «/» — по имени файла
    на любой глубине (как в .gitignore)."""
    m = mask.strip().replace("\\", "/").lstrip("/")
    out, i = "", 0
    while i < len(m):
        if m.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif m.startswith("**", i):
            out, i = out + ".*", i + 2
        elif m[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif m[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(m[i]), i + 1
    if "/" not in m:
        out = "(?:.*/)?" + out
    return re.compile("^" + out + "(?:/.*)?$")


def _matches(rel: str, rxs: list) -> bool:
    return any(rx.match(rel) for rx in rxs)


def language_of(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    low = name.lower()
    if low in NAMES:
        return NAMES[low]
    return LANGS.get(os.path.splitext(low)[1], "")


def classify(rel: str, head: bytes | None = None) -> str:
    """Почему файл не идёт в счёт кода — причина из `SKIP_REASONS` — или пусто. `head` — начало файла (его нет —
    решают только путь и имя)."""
    parts = rel.lower().split("/")
    name = parts[-1]
    if any(p in DEPS_DIRS for p in parts[:-1]):
        return "deps"
    try:
        import request_context as RC
        if RC.SECRET_NAME.match(name):
            return "secret"
    except ImportError:
        pass
    if name in LOCK_NAMES or name.endswith(".lock"):
        return "lock"
    ext = os.path.splitext(name)[1]
    if ext in BINARY_EXT:
        return "binary"
    if ".min." in name or ext == ".map":
        return "minified"
    if GENERATED_NAME.search(name):
        return "generated"
    if head is not None:
        if b"\0" in head[:8000]:
            return "binary"
        if GENERATED_HEAD.search(head[:1024]):
            return "generated"
    return ""


def repo_stats(target: Path, rec: dict | None = None) -> dict:
    """Языки и размер клона по отслеживаемым файлам рабочего дерева: {languages: {язык: {files, lines}}, files,
    counted, other, bytes, skipped: {причина: число}, longest_path}. Без внешних инструментов."""
    rec = rec or {}
    rc, out, _e = _git(target, ["ls-files", "-z"])
    rels = [r for r in out.split("\0") if r] if rc == 0 else []
    inc = [_mask_rx(m) for m in CREPO.masks(rec.get("include"))]
    exc = [_mask_rx(m) for m in CREPO.masks(rec.get("exclude"))]
    langs: dict = {}
    skipped = {r: 0 for r in SKIP_REASONS}
    other = counted = size = 0
    longest = max((len(r) for r in rels), default=0)
    for rel in rels:
        path = target.joinpath(*rel.split("/"))
        if exc and _matches(rel, exc) and not (inc and _matches(rel, inc)):
            skipped["excluded"] += 1
            continue
        forced = bool(inc) and _matches(rel, inc)
        why = "" if forced else classify(rel)
        if why:
            skipped[why] += 1
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue                          # вне рабочего дерева (частичная выгрузка) — не в счёт
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        if st.st_size > MAX_FILE_KB * 1024:
            skipped["too_big"] += 1
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        why = "" if forced else classify(rel, data[:8192])
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        if not why and lines and os.path.splitext(rel)[1].lower() in (".js", ".css") and len(data) / lines > 300:
            why = "minified"
        if why:
            skipped[why] += 1
            continue
        lang = language_of(rel)
        if not lang:
            other += 1
            continue
        row = langs.setdefault(lang, {"files": 0, "lines": 0})
        row["files"] += 1
        row["lines"] += lines
        counted += 1
        size += st.st_size
    ordered = dict(sorted(langs.items(), key=lambda kv: (-kv[1]["lines"], kv[0])))
    return {"languages": ordered, "files": len(rels), "counted": counted, "other": other, "bytes": size,
            "skipped": {k: v for k, v in skipped.items() if v}, "longest_path": longest}


# ---------------------------------------------------------------- проверка соединений

def net_verdicts(repos: dict) -> dict:
    """{имя: вердикт} проверки соединений для каждого репозитория; проверка выключена — всем «можно»."""
    ok = {n: {"ok": True, "blocked": []} for n in repos}
    if os.environ.get("AURORA_NET_GATE") == "0":
        return ok
    try:
        import conn_gate as G
        svc = G.CN.service()
        if not svc.enabled:
            return ok
    except Exception:  # noqa: BLE001 — нет модуля связи (старый движок): синк идёт, как раньше
        return ok
    out = {}
    for name, rec in repos.items():
        need = CREPO.access_needs(rec)
        if not need["profile"] and not need["internet"]:
            out[name] = ok[name]
            continue
        req = {"profiles": [need["profile"]] if need["profile"] else [], "internet": need["internet"],
               "sources": [], "offline_capable": False}
        try:
            out[name] = G.check_requirements(req, svc)
        except Exception:  # noqa: BLE001 — сбой проверки связи — подсказка, а не остановка
            out[name] = ok[name]
    return out


# ---------------------------------------------------------------- синхронизация одного репозитория

def target_rev(target: Path, rec: dict) -> tuple:
    """(ревизия, что это за цель, проблема) после `fetch`: закреплённая ревизия или вершина ветки сервера."""
    pin = rec.get("pin") or ""
    if pin:
        rev = _ok(target, "rev-parse", "-q", "--verify", pin + "^{commit}")
        return (rev, pin, None) if rev else ("", pin, problem("code_pin_absent"))
    branch = rec.get("branch") or ""
    ref = f"refs/remotes/origin/{branch}" if branch else "refs/remotes/origin/HEAD"
    rev = _ok(target, "rev-parse", "-q", "--verify", ref + "^{commit}")
    if not rev and not branch:
        heads = _ok(target, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").splitlines()
        if not heads:
            return "", "", problem("code_empty_remote")
        rev = _ok(target, "rev-parse", heads[0] + "^{commit}")
        return rev, heads[0].rsplit("/", 1)[-1], None
    if not rev:
        return "", branch, problem("remote_branch_missing")
    return rev, branch or (_ok(target, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1]), None


def sync_one(project, name: str, rec: dict, creds: dict, reset: bool = False) -> dict:
    """Склонировать или обновить один репозиторий и поставить на ревизию. → {ok, action, rev, prev, target, problem}."""
    target = CREPO.clone_path(project, name)
    url = rec["url"]
    cur = clone_state(project, name, rec)
    res = {"name": name, "ok": False, "action": "", "rev": "", "prev": cur.get("rev", ""), "target": ""}
    if cur["state"] in ("foreign", "dirty") and reset and cur.get("why") != "not_git":
        _reset(target, url)
        cur = clone_state(project, name, rec)
    if cur["state"] == "foreign":
        return {**res, "problem": problem("code_foreign_clone", cur.get("url", ""))}
    if cur["state"] == "dirty":
        return {**res, "problem": problem("code_dirty_clone", "\n".join(cur.get("dirty") or []))}
    if cur["state"] == "missing":
        made = not target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        rc, o, e = _git(target.parent, ["ls-remote", "--heads", url], rec, creds, network=True, timeout=120)
        if rc:
            tail = (o + "\n" + e).strip()
            return {**res, "problem": problem(GS.classify(tail), tail)}
        heads = [ln.split("refs/heads/", 1)[1] for ln in o.splitlines() if "refs/heads/" in ln]
        want = rec.get("branch") or ""
        if want and heads and want not in heads:
            return {**res, "problem": problem("remote_branch_missing")}
        args = ["clone", "--no-checkout", "-o", "origin", *CLONE_CFG, *(["-b", want] if want and heads else []),
                "--", url, target.name]
        rc, o, e = _git(target.parent, args, rec, creds, network=True, timeout=NET_TIMEOUT)
        if rc:
            if made and target.is_dir():
                shutil.rmtree(target, ignore_errors=True)      # своя недоделанная папка, а не чужие данные
            tail = (o + "\n" + e).strip()
            return {**res, "problem": problem(GS.classify(tail), tail)}
        res["action"] = "clone"
    else:
        rc, o, e = _git(target, ["fetch", "--prune", "--tags", "--force", "origin"], rec, creds, network=True,
                        timeout=NET_TIMEOUT)
        if rc:
            tail = (o + "\n" + e).strip()
            return {**res, "problem": problem(GS.classify(tail), tail)}
        res["action"] = "fetch"
    rev, what, prob = target_rev(target, rec)
    res["target"] = what
    if prob:
        return {**res, "problem": prob, "ok": prob["code"] == "code_empty_remote"}
    if _ok(target, "rev-parse", "-q", "--verify", "HEAD^{commit}") != rev \
            or _ok(target, "symbolic-ref", "-q", "HEAD"):
        rc, o, e = _git(target, ["checkout", "-q", "--force", "--detach", rev], timeout=NET_TIMEOUT)
        if rc:
            tail = (o + "\n" + e).strip()
            return {**res, "problem": problem(GS.classify(tail), tail)}
    return {**res, "ok": True, "rev": rev, "date": _ok(target, "log", "-1", "--format=%cI", rev)}


def _reset(target: Path, url: str) -> None:
    """Вернуть клон к реестру: адрес сервера — из реестра, правки и лишние файлы рабочего дерева — прочь.
    Только внутри клона: папки проекта это не касается."""
    if _ok(target, "remote"):
        _git(target, ["remote", "set-url", "origin", url])
    else:
        _git(target, ["remote", "add", "origin", url])
    _git(target, ["reset", "-q", "--hard"])
    _git(target, ["clean", "-q", "-fd"])


# ---------------------------------------------------------------- команды

def selected(project, only: str) -> tuple:
    """(реестр после проверки, выбранные имена, замечания реестра, неизвестное имя)."""
    clean, probs = CREPO.check(CREPO.read_repos(project))
    names = list(clean)
    if only:
        hit = CREPO.find(clean, only)
        if not hit:
            return clean, [], probs, only
        names = [hit]
    return clean, names, probs, ""


def plan_line(project, name: str, rec: dict, reset: bool) -> str:
    cur = clone_state(project, name, rec)
    goal = f"ревизию {rec['pin']}" if rec.get("pin") else (f"вершину ветки {rec['branch']}" if rec.get("branch")
                                                           else "вершину ветки по умолчанию")
    where = f"{CODE_DIR}/{name}"
    if cur["state"] == "missing":
        return f"· {name}: склонирую {rec['url']} в {where} и поставлю на {goal}"
    if cur["state"] in ("foreign", "dirty") and reset and cur.get("why") != "not_git":
        return (f"· {name}: сброшу клон к реестру (правок в клоне: {cur.get('dirty_count', 0)}), заберу свежее и "
                f"поставлю на {goal}")
    if cur["state"] == "foreign":
        p = problem("code_foreign_clone")
        return f"✗ {name}: {p['title']} — {p['fix']}"
    if cur["state"] == "dirty":
        p = problem("code_dirty_clone")
        return f"✗ {name}: {p['title']} ({cur.get('dirty_count', 0)}) — {p['fix']}"
    return f"· {name}: заберу свежее с сервера и поставлю на {goal} (сейчас {cur['rev'][:8]})"


def run_sync(project, only: str = "", apply: bool = False, reset: bool = False, as_json: bool = False) -> int:
    clean, names, probs, unknown = selected(project, only)
    if unknown:
        _say(f"✗ репозитория «{unknown}» нет в реестре кода — подключите его в разделе «Код»")
        return 2
    bad = {p["name"] for p in probs}
    for p in probs:
        _say(f"✗ {p['name']}: запись реестра не годится — поле {p['field']} ({p['code']}); поправьте в разделе «Код»")
    names = [n for n in names if n not in bad]
    if not names:
        if not probs:
            _say("Репозиториев кода в реестре нет — синхронизировать нечего (подключаются в разделе «Код»).")
            if as_json:
                _say(json.dumps({"repos": []}, ensure_ascii=False))
            return 0
        return 1
    if reset and not only:
        _say("✗ «Сбросить клон» — только для одного репозитория: добавьте --repo <имя>")
        return 2
    if not apply:
        _say(f"code:sync — план, репозиториев {len(names)}:")
        for n in names:
            _say(plan_line(project, n, clean[n], reset))
        _say("Ничего не записано: это план. Клонировать и обновить — с --apply.")
        return 1 if bad else 0
    if not GS.has_git():
        p = GS.problem("no_git")
        _say(f"✗ {p['title']} — {p['fix']}")
        return 2
    lock = lock_file(project)
    lock.parent.mkdir(parents=True, exist_ok=True)
    taken, holder = lock_take(str(lock), {"task": "code:sync"})
    if not taken:
        _say(f"✗ синхронизация кода уже идёт (процесс {holder.get('pid', '?')}) — дождитесь её конца")
        return 2
    try:
        return _sync_all(project, clean, names, reset, as_json, bool(bad))
    finally:
        lock_release(str(lock))


def _sync_all(project, clean: dict, names: list, reset: bool, as_json: bool, had_bad: bool) -> int:
    verdicts = net_verdicts({n: clean[n] for n in names})
    st = read_state(project)
    repos_st = st.setdefault("repos", {})
    results, blocked, failed = [], [], had_bad
    _say(f"code:sync — репозиториев {len(names)}")
    for name in names:
        rec = clean[name]
        v = verdicts.get(name) or {"ok": True}
        if not v.get("ok", True):
            blocked.append((name, v))
            continue
        res = sync_one(project, name, rec, CREPO.creds_of(project, name), reset)
        entry = repos_st.setdefault(name, {})
        entry.update(at=utc_stamp(), ok=res["ok"], action=res["action"],
                     problem=(res.get("problem") or {}).get("code", ""))
        if res["ok"] and res.get("rev"):
            changed = entry.get("rev") != res["rev"]
            entry.update(rev=res["rev"], date=res.get("date", ""), target=res["target"], url=rec["url"])
            if changed or (entry.get("stats") or {}).get("rev") != res["rev"]:
                entry["stats"] = {"rev": res["rev"], **repo_stats(CREPO.clone_path(project, name), rec)}
            res["changed"] = changed
        results.append(res)
        _say(result_line(res))
        failed = failed or not res["ok"]
    for gone in [n for n in repos_st if not CREPO.find(clean, n)]:
        repos_st.pop(gone, None)                 # убранный из реестра репозиторий: состояние о нём больше не нужно
    st["at"] = utc_stamp()
    _write_state(project, st)
    if as_json:
        _say(json.dumps({"repos": results}, ensure_ascii=False))
    if blocked:
        import conn_gate as G
        for name, v in blocked:
            _say(f"✗ {name}: не синхронизирован — " + "; ".join(G.reason_line(r) for r in v["blocked"])
                 + ". Это отказ проверки соединений: связь вернётся — запустите снова (состояние — net:status).")
        rows = [r for _n, v in blocked for r in v["blocked"]]
        _say(G.net_marker(rows, [f"code:{n}" for n, _v in blocked]))
        return 2
    return 1 if failed else 0


def result_line(res: dict) -> str:
    if not res["ok"]:
        p = res.get("problem") or {}
        line = f"✗ {res['name']}: {p.get('title', 'не синхронизирован')} — {p.get('fix', '')}"
        raw = (p.get("raw") or "").strip()
        return line + ("\n    " + "\n    ".join(raw.splitlines()[-6:]) if raw else "")
    if not res.get("rev"):
        p = res.get("problem") or {}
        return f"✓ {res['name']}: {p.get('title', 'пусто')}"
    what = {"clone": "склонирован", "fetch": "обновлён" if res.get("changed") else "без изменений"}.get(res["action"], "")
    date = (res.get("date") or "")[:10]
    return f"✓ {res['name']}: {what}, ревизия {res['rev'][:8]} ({res.get('target') or '—'}{', ' + date if date else ''})"


def status(project, only: str = "") -> dict:
    """Состояние без сети: по каждому репозиторию — клон, ревизия, отставание от ветки сервера на момент последнего
    обновления, последняя синхронизация, языки и размер. → {repos: [...], problems: [...]}."""
    clean, names, probs, unknown = selected(project, only)
    st = read_state(project).get("repos") or {}
    rows = []
    for name in names:
        rec = clean[name]
        cur = clone_state(project, name, rec)
        row = {"name": name, "url": rec["url"], "branch": rec["branch"], "pin": rec["pin"], "models": rec["models"],
               "publish": rec["publish"], "access": rec["access"], "state": cur["state"], "rev": cur.get("rev", ""),
               "dirty": cur.get("dirty_count", 0), "last": {k: (st.get(name) or {}).get(k, "")
                                                            for k in ("at", "ok", "action", "problem")}}
        target = CREPO.clone_path(project, name)
        if cur["state"] in ("ok", "dirty") and cur.get("rev"):
            row["date"] = _ok(target, "log", "-1", "--format=%cI", cur["rev"])
            branch = rec["branch"] or _ok(target, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").split("/", 1)[-1]
            tip = _ok(target, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}^{{commit}}") if branch else ""
            row["tracking"] = branch
            row["behind"] = int(_ok(target, "rev-list", "--count", f"{cur['rev']}..{tip}") or 0) if tip else None
            cached = (st.get(name) or {}).get("stats") or {}
            row["stats"] = cached if cached.get("rev") == cur["rev"] else {"rev": cur["rev"], **repo_stats(target, rec)}
            longest = row["stats"].get("longest_path") or 0
            full = len(str(target.resolve())) + 1 + longest
            row["path_overflow"] = full > WIN_PATH
        rows.append(row)
    return {"repos": rows, "problems": probs, "unknown": unknown}


def run_status(project, only: str = "", as_json: bool = False) -> int:
    data = status(project, only)
    if data["unknown"]:
        _say(f"✗ репозитория «{data['unknown']}» нет в реестре кода")
        return 2
    if as_json:
        _say(json.dumps(data, ensure_ascii=False))
        return 0
    if not data["repos"] and not data["problems"]:
        _say("Репозиториев кода в реестре нет (подключаются в разделе «Код»).")
        return 0
    words = {"missing": "не склонирован — code:sync --apply", "foreign": "в папке не клон этого репозитория",
             "dirty": "в клоне есть правки — «Сбросить клон»", "ok": ""}
    for r in data["repos"]:
        head = f"{r['name']}: {r['url']}"
        if r["state"] != "ok":
            _say(f"{head}\n  ✗ {words.get(r['state'], r['state'])}")
            if r["state"] != "dirty":
                continue
        behind = r.get("behind")
        if behind is None:
            lag = ""
        elif behind == 0:
            lag = " · вровень с сервером"
        elif r["pin"]:
            lag = f" · ветка {r.get('tracking')} ушла вперёд от закреплённой ревизии на {behind} коммит(ов)"
        else:
            lag = f" · отстаёт от сервера на {behind} коммит(ов) — code:sync --apply"
        _say(f"{head}\n  ревизия {r['rev'][:8]} от {(r.get('date') or '')[:10]} · цель {r['pin'] or r.get('tracking') or '—'}"
             f"{lag}")
        last = r["last"]
        if last.get("at"):
            _say(f"  последняя синхронизация {last['at']}" + ("" if last.get("ok") else f" — не прошла ({last.get('problem')})"))
        s = r.get("stats") or {}
        langs = ", ".join(f"{k} {v['files']} ф. / {v['lines']} стр." for k, v in (s.get("languages") or {}).items())
        _say(f"  языки: {langs or '—'}")
        _say(f"  размер: файлов в репозитории {s.get('files', 0)}, кода {s.get('counted', 0)} "
             f"({round((s.get('bytes') or 0) / 1024)} КБ), прочих {s.get('other', 0)}")
        if s.get("skipped"):
            _say("  не в счёт: " + ", ".join(f"{k} {v}" for k, v in s["skipped"].items()))
        if r.get("path_overflow"):
            _say(f"  ⚠ самый длинный путь клона не поместится на Windows без длинных путей ({WIN_PATH} знаков): "
                 "проверьте kit:paths")
        _say(f"  моделям: {'можно' if r['models'] else 'нельзя'} · публиковать по коду: {'можно' if r['publish'] else 'нельзя'}")
    for p in data["problems"]:
        _say(f"✗ {p['name']}: запись реестра не годится — поле {p['field']} ({p['code']})")
    return 1 if data["problems"] else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Клоны репозиториев кода в Code/: синхронизация и состояние")
    ap.add_argument("--sync", action="store_true", help="клонировать или обновить репозитории кода (без --apply — план)")
    ap.add_argument("--status", action="store_true", help="состояние: ревизия, отставание от сервера, языки и размер")
    ap.add_argument("--repo", default="", metavar="ИМЯ", help="только этот репозиторий из реестра")
    ap.add_argument("--reset", action="store_true",
                    help="с --sync и --repo: сбросить клон к реестру — правки и лишние файлы клона прочь, адрес из реестра")
    ap.add_argument("--apply", action="store_true", help="записать: клонировать и обновить (без него — только план)")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    a = ap.parse_args(argv)
    project = os.getcwd()
    if not (Path(project) / CREPO.CONFIG).is_file():
        _say("✗ здесь нет aurora.config.yaml — запустите из корня проекта")
        return 2
    if a.status:
        return run_status(project, a.repo, a.json)
    return run_sync(project, a.repo, a.apply, a.reset, a.json)


if __name__ == "__main__":
    sys.exit(main())
