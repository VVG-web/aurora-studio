#!/usr/bin/env python3
"""aurora_extras.py — надстройки движка: Pydantic AI и graphify (фреймворк «Аврора»).

Ядро кита работает на одной стандартной библиотеке — это его принцип. Надстройки
делают работу лучше, но не обязательны: Pydantic AI — встроенный агент (проверка ответа
модели, инструменты, MCP), graphify — граф базы (темы Лейденом, MCP-сервер графа для
агентов, выгрузки, разбор кода и SQL). Каждая живёт в своём venv под `~/.aurora/`: их
зависимости не протекают ни в движок, ни друг в друга.

  python3 aurora_extras.py --status            # что стоит, что вышло
  python3 aurora_extras.py --status --json
  python3 aurora_extras.py --install graphify  # поставить или обновить
  python3 aurora_extras.py --install graphify --no-quarantine   # без паузы после выпуска (CI, срочное исправление)

Версия «в git» — последний выпуск репозитория надстройки на GitHub: так пользователь
видит, что вышло. Ставится она через pip с PyPI; если PyPI от git отстаёт, статус
называет обе версии честно.

Установка не берёт «что вышло час назад»: набор пакетов решается заранее, сама надстройка
не моложе `QUARANTINE_DAYS` суток (её зависимости выбирает pip), ставится ровно решённый набор по хешам, и его
снимок остаётся в `~/.aurora/locks/` — по нему venv откатывается целиком (BL-SEC-21). Пауза мягкая: нельзя
применить — ставится без неё, и об этом сказано в журнале и в итоге.

Панель: «Установка» → «Надстройки движка» (кнопки «Установить» / «Обновить»).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    # и портят протокол MCP; движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
import time
import urllib.error
import urllib.request
from pathlib import Path

from aurora_common import replace_file, venv_env  # noqa: E402

HOME = Path.home() / ".aurora"
CACHE = HOME / "extras-cache.json"
CACHE_TTL = 6 * 3600            # как у проверки версии кита: сеть трогаем редко
# Цепочка поставок (BL-SEC-21): надстройки ставятся в venv на машине, где лежат токены Jira и Confluence,
# а свежие выпуски популярных пакетов бывают скомпрометированы и снимаются в первые дни.
QUARANTINE_DAYS = 7             # выпуск моложе — не ставится (как `cooldown` dependabot в репозитории кита)
LOCKS = HOME / "locks"          # снимки установок: <LOCKS>/<надстройка>/<UTC>-<вид>.txt, формат requirements с хешами
LOCK_KEEP = 5                   # сколько снимков хранить на надстройку
RESOLVE_ROUNDS = 6              # сколько раз пересобирать набор, отбрасывая слишком свежие выпуски
PYPI_VERSION_JSON = "https://pypi.org/pypi/{name}/{version}/json"

EXTRAS = {
    "pydantic-ai": {
        "title": "Pydantic AI",
        "dist": "pydantic-ai",
        "pip": ["pydantic-ai"],
        "venv": HOME / "venv",
        "repo": "pydantic/pydantic-ai",
        "min_python": (3, 10),      # Pydantic AI с 1.0 требует 3.10+ (на 3.9 pip берёт старую ветку 0.x)
        "enables": "каждый вызов модели идёт через него: ответ проверяется, "
                   "инструменты и MCP-серверы в задачах",
    },
    "graphify": {
        "title": "graphify",
        "dist": "graphifyy",
        # С Лейденом и MCP-сервером графа; что не ставится на этой машине — без того.
        # `tree-sitter-sql` — грамматика SQL: без неё разбор SQL молча даёт ноль узлов.
        "pip": ["graphifyy[leiden,mcp] tree-sitter-sql", "graphifyy[mcp] tree-sitter-sql",
                "graphifyy"],
        "venv": HOME / "graphify",
        "repo": "Graphify-Labs/graphify",
        "min_python": (3, 10),
        "enables": "граф базы: темы Лейденом, MCP-сервер графа для Claude Code и Cursor, "
                   "выгрузки HTML и Neo4j, разбор кода и SQL проекта",
    },
}


def venv_python(extra_id: str) -> Path:
    venv = EXTRAS[extra_id]["venv"]
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(cmd: list, timeout: int = 60) -> subprocess.CompletedProcess:
    # PYTHONHOME/PYTHONPATH чужого Python окружения в venv не попадают (`venv_env`): с ними pip и зонды
    # venv видели бы чужую стандартную библиотеку и чужие пакеты
    env = venv_env({k: v for k, v in os.environ.items() if not k.startswith("Malloc")})
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)


def installed_version(extra_id: str) -> str:
    """Версия, стоящая в venv надстройки. Пусто — не стоит.
    `os.path.isfile`, а не `Path.is_file`: ссылка venv на Python из закрытого чужого профиля даёт отказ в доступе
    (до Python 3.12 — исключение), а это «для вас не стоит», а не падение «Установки»."""
    vpy = venv_python(extra_id)
    if not os.path.isfile(vpy):
        return ""
    dist = EXTRAS[extra_id]["dist"]
    try:
        p = _run([str(vpy), "-I", "-c", "from importlib.metadata import version; "
                  f"print(version({dist!r}))"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _get_json(url: str, timeout: int = 15, limit: int = 2_000_000) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "aurora-studio",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"ответ {url.split('?')[0]} больше {limit >> 20} МБ — не разбираю")
    return json.loads(raw.decode("utf-8", "replace"))


def _clean(tag: str) -> str:
    """`v0.9.69` → `0.9.69`; всё, что не похоже на номер версии, — как есть."""
    m = re.search(r"\d+(?:\.\d+)+(?:[-.]?(?:a|b|rc|post|dev)\d*)?", tag or "")
    return m.group(0) if m else (tag or "")


def latest(extra_id: str, fresh: bool = False) -> dict:
    """{git, pypi, error} — последний выпуск на GitHub и версия на PyPI. Кэш 6 часов."""
    cache = {}
    try:
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    got = cache.get(extra_id) or {}
    if not fresh and got and time.time() - got.get("at", 0) < CACHE_TTL:
        return got
    ex = EXTRAS[extra_id]
    out = {"git": "", "pypi": "", "aged": "", "error": "", "at": time.time()}
    try:
        rel = _get_json(f"https://api.github.com/repos/{ex['repo']}/releases/latest")
        out["git"] = _clean(rel.get("tag_name") or rel.get("name") or "")
    except Exception as e:                                   # noqa: BLE001
        try:        # выпусков нет — берём последний тег
            tags = _get_json(f"https://api.github.com/repos/{ex['repo']}/tags?per_page=1")
            out["git"] = _clean(tags[0]["name"]) if tags else ""
        except Exception:                                    # noqa: BLE001
            out["error"] = f"GitHub не ответил: {str(e)[:120] or type(e).__name__}"
    try:
        data = _get_json(f"https://pypi.org/pypi/{ex['dist']}/json", limit=30_000_000)
        out["pypi"] = data["info"]["version"]
        out["aged"] = aged_from(data, QUARANTINE_DAYS)       # то, что поставит установка с паузой
    except Exception as e:                                   # noqa: BLE001
        out["error"] = (out["error"] + "; " if out["error"] else "") + \
            f"PyPI не ответил: {str(e)[:120] or type(e).__name__}"
    cache[extra_id] = out
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return out


def _vt(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def status(fresh: bool = False, online: bool = True) -> list:
    """Строка на надстройку: что стоит, что вышло в git и на PyPI, можно ли обновить."""
    rows = []
    for eid, ex in EXTRAS.items():
        have = installed_version(eid)
        new = latest(eid, fresh) if online else {"git": "", "pypi": "", "aged": "", "error": ""}
        avail = new.get("pypi") or ""
        # Обновить можно до того, что поставит установка: выпуск старше паузы, а не самый свежий на PyPI
        # (кэш прежнего выпуска кита поля нет — тогда как раньше, до версии PyPI).
        target = (new.get("aged") if "aged" in new else avail) or ""
        rows.append({
            "id": eid, "title": ex["title"], "enables": ex["enables"],
            "installed": have, "git": new.get("git", ""), "pypi": avail, "target": target or avail,
            "error": new.get("error", ""),
            "update": bool(have and target and _vt(target) > _vt(have)),
            "pypi_behind": bool(new.get("git") and avail and _vt(new["git"]) > _vt(avail)),
            "venv": str(ex["venv"]), "repo": f"https://github.com/{ex['repo']}",
            "python": python_for(eid) or "",
            **({"mcp": _graph_mcp_current()} if eid == "graphify" and have else {}),
            **({"compat": compat_of(have)} if eid == "pydantic-ai" and have else {}),
        })
    return rows


def _graph_mcp_current() -> dict:
    """Запись сервера графа — и прежнюю, от 1.138, приводит к нынешней: это наша запись."""
    m = graph_mcp()
    if m and not m.get("registered"):
        try:
            servers = json.loads(kit_mcp_file().read_text(encoding="utf-8")).get("mcpServers") or {}
        except (OSError, ValueError, AttributeError):
            servers = {}
        if GRAPH_MCP in servers and not register_graph_mcp():
            m = graph_mcp()
    return m


def compat_of(version: str):
    """Запомненная проверка совместимости для этой версии. None — ещё не проверялась."""
    try:
        import agent_core as AG
    except ImportError:
        return None
    chk = AG.last_selfcheck()
    if chk.get("version") != version:
        return None
    return {"ok": bool(chk.get("ok")), "problems": chk.get("problems") or [], "at": chk.get("at", "")}


def python_for(extra_id: str) -> str:
    """Питон, на котором можно поставить надстройку: этот, если подходит. Пусто — не подходит.

    Ниже 3.10 движок не запускается вовсе (защита версии в aurora_common останавливает процесс
    при импорте), поэтому поиск другого интерпретатора здесь недостижим и не нужен."""
    need = EXTRAS[extra_id]["min_python"]
    return sys.executable if sys.version_info[:2] >= need else ""


def check_compat(version: str) -> dict:
    """Самопроверка адаптера Pydantic AI для этой версии (заново, без памяти)."""
    try:
        import agent_core as AG
    except ImportError:
        return {"ok": True, "problems": [], "version": version}
    return AG.adapter_selfcheck(version, force=True)


# ------------------------------------------------------------------ закрепление и карантин (BL-SEC-21)
#
# Раньше `install()` делал `pip install --upgrade <пакет>`: что вышло час назад на PyPI, то и попадало в venv вместе
# со всем деревом зависимостей, без хешей и без паузы, а откат возвращал один верхний пакет. Теперь установка идёт
# в три шага: (1) pip только РЕШАЕТ набор (`--dry-run --report`), (2) прямой пакет, выпущенный моложе
# `QUARANTINE_DAYS`, запрещается (`имя<версия`) и набор решается заново (зависимости выбирает pip: у дерева из десятков
# пакетов всегда есть вчерашний выпуск, и пауза на всех сделала бы установку невозможной; нельзя применить — набор решается
# без паузы, с заметной строкой), (3) ставится ровно решённый набор из файла с хешами (`--require-hashes --no-deps`).
# Этот файл — снимок установки: по нему откатывается venv целиком.

def _now() -> float:
    return time.time()


def _get_age_json(url: str):
    """JSON страницы выпуска на PyPI; `None` — такого выпуска там нет (частный индекс)."""
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _epoch(stamp: str) -> float:
    """`2026-10-12T10:00:00.123456Z` → секунды UTC (без `fromisoformat`: он не знает «Z» до Python 3.11)."""
    import calendar
    import datetime
    dt = datetime.datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S")
    return float(calendar.timegm(dt.timetuple()))


def release_age_days(name: str, version: str, sha256: str = "", now: float = 0.0):
    """Сколько суток назад вышел этот файл выпуска. None — PyPI такого выпуска не знает (частный индекс).

    Файл — по хешу из решённого набора; не нашёлся — самый СВЕЖИЙ файл выпуска (дозагруженное позже колесо не
    должно делать выпуск «старым»). Ответ без времени загрузки — ValueError: возраст неизвестен, не «пускаем»."""
    data = _get_age_json(PYPI_VERSION_JSON.format(name=urllib.parse.quote(name, safe=""),
                                                  version=urllib.parse.quote(version, safe="")))
    if data is None:
        return None
    files = [u for u in (data.get("urls") or []) if isinstance(u, dict) and u.get("upload_time_iso_8601")]
    if not files:
        raise ValueError(f"PyPI не назвал время загрузки {name} {version}")
    mine = [u for u in files if sha256 and (u.get("digests") or {}).get("sha256") == sha256] or files
    return ((now or _now()) - max(_epoch(u["upload_time_iso_8601"]) for u in mine)) / 86400


def aged_from(data: dict, days: float, now: float = 0.0) -> str:
    """Самый новый выпуск из ответа PyPI старше `days` суток (без предвыпусков и снятых). Пусто — не нашёлся."""
    best, best_key = "", ()
    for version, files in (data.get("releases") or {}).items():
        files = [f for f in (files or []) if isinstance(f, dict) and not f.get("yanked")
                 and f.get("upload_time_iso_8601")]
        if not files or re.search(r"[a-zA-Z]", version):
            continue
        age = ((now or _now()) - max(_epoch(f["upload_time_iso_8601"]) for f in files)) / 86400
        if age >= days and _vt(version) > best_key:
            best, best_key = version, _vt(version)
    return best


def aged_version(dist: str, days: float, now: float = 0.0) -> str:
    """Самый новый выпуск `dist` старше `days` суток. Пусто — не нашёлся."""
    return aged_from(_get_json(f"https://pypi.org/pypi/{urllib.parse.quote(dist, safe='')}/json",
                               limit=30_000_000), days, now)


def _report_items(path) -> list:
    """Решённый pip набор → [{name, version, sha256}]. Ошибка формы — ValueError: ставить вслепую нельзя."""
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    items = []
    for it in report.get("install") or []:
        meta, dl = it.get("metadata") or {}, it.get("download_info") or {}
        name, version = meta.get("name"), meta.get("version")
        arc = dl.get("archive_info") or {}
        digest = (arc.get("hashes") or {}).get("sha256") or str(arc.get("hash") or "").partition("sha256=")[2]
        if not (name and version and digest):
            raise ValueError(f"у {name or '?'} {version or ''} нет хеша из индекса (каталог или VCS?) — "
                             "ставить без хеша не буду")
        # Отчёт пишет pip по ответам индекса: строка с переводом строки породила бы в файле снимка чужую директиву.
        if not (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", str(name)) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+!_-]*", str(version))
                and re.fullmatch(r"[0-9a-f]{64}", str(digest))):
            raise ValueError(f"в отчёте pip странное имя, версия или хеш пакета «{str(name)[:40]}» — не ставлю")
        items.append({"name": re.sub(r"[-_.]+", "-", name).casefold(), "version": version, "sha256": digest,
                      "url": str(dl.get("url") or "")})
    if not items:
        raise ValueError("pip не назвал ни одного пакета для установки")
    return sorted(items, key=lambda x: x["name"])


BLOCKED_PACKAGES = ("litellm", "dspy")      # решение по цепочке поставок: в надстройки не вводятся (ни прямо, ни через зависимость)


_NO_WHEEL = re.compile(r"No matching distribution found|Could not find a version that satisfies")
_NET_TROUBLE = re.compile(r"(?i)\bretrying \(|connection (?:error|broken|aborted|refused|reset)|timed out|read timeout|"
                          r"temporary failure|name resolution|ssl: |sslerror|proxyerror|certificate verify|http error|"
                          r"network is unreachable|max retries")


def _direct_names(spec: str) -> set:
    """Имена пакетов, которые названы в `spec` прямо (`graphifyy[mcp] tree-sitter-sql` → graphifyy, tree-sitter-sql)."""
    out = set()
    for token in spec.split():
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", token)
        if m:
            out.add(re.sub(r"[-_.]+", "-", m.group(0)).casefold())
    return out


def plan_lock(vpy, spec: str, days: float, now: float = 0.0, binary_only: bool = True, log=None) -> tuple:
    """Решить набор пакетов для `spec`; пауза в `days` суток — для пакетов, названных в `spec`. → (текст снимка, "") либо ("", причина).

    Пауза мягкая. Слишком свежий прямой пакет запрещается ограничением `имя<версия`, и pip решает набор заново: если
    у него есть замена постарше, получится набор из старых. Зависимости этих пакетов выбирает pip и в паузу не входят
    (у дерева из десятков пакетов всегда есть выпуск вчерашний: с паузой на всех установка была бы невозможна); они
    ставятся по хешам. Если паузу применить нельзя (нет старой замены, набор не решается с ограничением, PyPI не
    сказал возраст), набор решается без неё, а в `log` стоит строка «пауза зависимостей не применена»: отказ только
    когда не вышло и без паузы.
    Набор решается только по готовым колёсам (`--only-binary=:all:`): сборка пакета из исходников исполняет его код
    ещё до проверки возраста. Колеса нет — один повтор без этого запрета, но не при обрыве связи; в `log` сказано,
    какие пакеты пойдут из исходников (их сборка исполняет чужой код: версия и хеш при этом проверены так же)."""
    direct = _direct_names(spec)

    def soft(reason: str) -> tuple:
        if log is not None:
            log.append(f"⚠️ пауза зависимостей не применена: {reason}; ставлю без паузы")
        return plan_lock(vpy, spec, 0, now, binary_only, log)

    work = tempfile.mkdtemp(prefix="aurora-extras-")
    bans, ages = {}, {}
    try:
        for _round in range(RESOLVE_ROUNDS):
            report = os.path.join(work, "report.json")
            cmd = [str(vpy), "-I", "-X", "utf8", "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet",
                   "--disable-pip-version-check", "--report", report, *(["--only-binary=:all:"] if binary_only else []),
                   *spec.split()]
            if bans:
                cons = os.path.join(work, "constraints.txt")
                with open(cons, "w", encoding="utf-8") as f:
                    f.write("".join(f"{n}<{v}\n" for n, v in sorted(bans.items())))
                cmd += ["-c", cons]
            if os.path.exists(report):
                os.remove(report)
            p = _run(cmd, timeout=1800)
            if p.returncode != 0:
                said = (p.stderr or "") + (p.stdout or "")
                if binary_only and _NO_WHEEL.search(said) and not _NET_TROUBLE.search(said):
                    if log is not None:
                        log.append("у какого-то пакета нет готового колеса — набор решаю ещё раз, разрешив сборку из исходников")
                    return plan_lock(vpy, spec, days, now, False, log)   # у какого-то пакета нет колеса — один повтор
                if bans and not _NET_TROUBLE.search(said):
                    return soft("набор с ограничением по возрасту не решается (" + (said.strip()[-120:] or "pip не ответил") + ")")
                return "", ((p.stderr or p.stdout or "pip не ответил")[-300:]).strip()
            try:
                items = _report_items(report)
            except (OSError, ValueError) as e:
                return "", f"набор не решён: {e}"
            blocked = [it["name"] for it in items if it["name"] in BLOCKED_PACKAGES]
            if blocked:
                return "", f"в наборе пакет {blocked[0]}, который в надстройки не вводится (цепочка поставок)"
            young = []
            if days and direct:
                from concurrent.futures import ThreadPoolExecutor
                mine = [it for it in items if it["name"] in direct]
                todo = [it for it in mine if (it["name"], it["version"]) not in ages]
                try:
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        for it, age in zip(todo, pool.map(lambda i: release_age_days(
                                i["name"], i["version"], i["sha256"], now), todo)):
                            ages[(it["name"], it["version"])] = age
                except Exception as e:                     # noqa: BLE001 — сеть, ответ PyPI: возраст не узнать
                    return soft(f"не удалось узнать возраст выпусков на PyPI ({str(e)[:100] or type(e).__name__})")
                unknown = [it for it in mine if ages.get((it["name"], it["version"])) is None
                           and "files.pythonhosted.org" in it.get("url", "")]
                if unknown:        # файл с pythonhosted.org, а PyPI о выпуске молчит: возраст неизвестен
                    return soft(f"PyPI не знает выпуска {unknown[0]['name']} {unknown[0]['version']} — возраст неизвестен")
                young = [it for it in mine
                         if ages.get((it["name"], it["version"])) is not None
                         and ages[(it["name"], it["version"])] < days]
            if not young:
                if log is not None and not binary_only:
                    src = [it["name"] for it in items if not str(it.get("url", "")).endswith(".whl")]
                    if src:
                        log.append("из исходников соберутся: " + ", ".join(src[:8]) + " (сборка исполняет код пакета)")
                if log is not None and days:
                    rest = len([it for it in items if it["name"] not in direct])
                    if rest:
                        log.append(f"зависимости ({rest}) выбирает pip: пауза в {days:g} сут. на них не распространяется, "
                                   "они ставятся по хешам")
                return _lock_text(items, days, now, all(it.get("url", "").endswith(".whl") for it in items)), ""
            for it in young:
                bans[it["name"]] = it["version"]
        names = ", ".join(f"{n} {v}" for n, v in sorted(bans.items())[:6])
        return soft(f"не нашлось набора без выпусков моложе {days:g} сут. за {RESOLVE_ROUNDS} проходов (мешают: {names})")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _lock_text(items: list, days: float, now: float = 0.0, wheels: bool = False) -> str:
    from aurora_common import utc_slug
    stamp = utc_slug("%Y-%m-%dT%H:%M:%S", now or _now())
    # заголовок только ASCII: pip читает файл в кодировке локали
    head = [f"# Aurora extras install snapshot {stamp}",
            f"# release wait: {days:g} days; python {sys.version_info[0]}.{sys.version_info[1]}"
            + ("; only-binary" if wheels else "")]
    body = [f"{it['name']}=={it['version']} \\\n    --hash=sha256:{it['sha256']}" for it in items]
    return "\n".join(head + body) + "\n"


def lock_files(extra_id: str) -> list:
    """Снимки надстройки, новые первыми."""
    folder = LOCKS / extra_id
    return sorted(folder.glob("*.txt"), reverse=True) if folder.is_dir() else []


def save_lock(extra_id: str, text: str, kind: str = "install") -> Path:
    """Положить снимок в `~/.aurora/locks/<надстройка>/` (только владельцу), лишние старые убрать."""
    folder = LOCKS / extra_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    from aurora_common import utc_slug
    base = utc_slug("%Y%m%dT%H%M%S")
    # номер внутри секунды: имена сортируются по времени, а не по виду снимка; берём следующий за наибольшим,
    # а не первый свободный — иначе место прибранного старого снимка занял бы новый и встал бы в хвост
    taken = [int(m.group(1)) for f in folder.glob(f"{base}-*.txt")
             for m in [re.match(r".*Z-(\d{3})-", f.name)] if m]
    n = max(taken, default=0) + 1
    while True:                 # два процесса не получат одно имя: файл создаётся только если его ещё нет
        path = folder / f"{base}-{n:03d}-{kind}.txt"
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(text)
            break
        except FileExistsError:
            n += 1
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    for old in lock_files(extra_id)[LOCK_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def freeze_text(vpy) -> str:
    """Что стоит в venv сейчас — `имя==версия` без хешей (старая установка снимка не оставила)."""
    try:
        p = _run([str(vpy), "-I", "-X", "utf8", "-m", "pip", "freeze", "--all", "--disable-pip-version-check"], timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    if p.returncode != 0:
        return ""
    rows = [ln.strip() for ln in (p.stdout or "").splitlines() if re.match(r"^[A-Za-z0-9][\w.\-]*==\S+$", ln.strip())]
    return "# Aurora extras snapshot before the update (no hashes)\n" + "\n".join(rows) + "\n" if rows else ""


_LOCK_LINE = re.compile(r"(?:#.*|[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+(?:\s+--hash=sha256:[0-9a-f]{64})*(?:\s*\\)?"
                        r"|\s+--hash=sha256:[0-9a-f]{64}(?:\s*\\)?|\s*)")


def lock_text_problem(text: str) -> str:
    """Снимок читают с диска и отдают pip: в нём допустимы только комментарии, `имя==версия` и `--hash=sha256:…`.

    Опция вроде `--index-url` или `-e` в таком файле сработала бы как директива pip, а без `--hash` проверки
    хеша нет — файл правили руками. → описание первой странной строки либо пусто."""
    for i, line in enumerate(text.splitlines(), 1):
        if not _LOCK_LINE.fullmatch(line):
            return f"в снимке странная строка {i}: «{line.strip()[:60]}»"
    return ""


def _lock_version(path, dist: str) -> str:
    """Версия пакета `dist` в снимке; пакета нет или файл не прочитать — пусто."""
    want = re.sub(r"[-_.]+", "-", dist).casefold()
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!_-]+)", line)
            if m and re.sub(r"[-_.]+", "-", m.group(1)).casefold() == want:
                return m.group(2)
    except (OSError, ValueError):
        pass
    return ""


def _install_lock(vpy, path) -> subprocess.CompletedProcess:
    text = Path(path).read_text(encoding="utf-8")
    bad = lock_text_problem(text)
    if bad:
        return subprocess.CompletedProcess([], 2, "", bad + " — снимок не используется")
    hashed = "--hash=" in text
    wheels = bool(re.search(r"(?m)^# release wait: .*; only-binary\s*$", text))
    return _run([str(vpy), "-I", "-X", "utf8", "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps",
                 *(["--require-hashes"] if hashed else []), *(["--only-binary=:all:"] if wheels else []),
                 "-r", str(path)], timeout=1800)


def restore(extra_id: str, lock: Path, log: list) -> bool:
    """Вернуть venv целиком по снимку: новый venv на месте старого, старый уходит в сторону и возвращается при сбое."""
    ex = EXTRAS[extra_id]
    venv = ex["venv"]
    base = python_for(extra_id)
    if not base or not lock.is_file():
        return False
    # имя уникально на попытку: остаток прежней (занятый файл, антивирус) не примут за отложенный сейчас venv
    for old in venv.parent.glob(venv.name + ".prev-*"):
        shutil.rmtree(old, ignore_errors=True)
    aside = venv.with_name(f"{venv.name}.prev-{os.getpid()}-{int(_now())}")
    existed = venv.exists()
    moved = False
    ok = False
    try:
        if existed:
            os.replace(venv, aside)         # на Windows venv, занятый процессом, не переименуется — тогда он остаётся как был
            moved = True
        made = _run([base, "-I", "-X", "utf8", "-m", "venv", str(venv)], timeout=300)
        ok = made.returncode == 0 and _install_lock(venv_python(extra_id), lock).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.append(f"откат по снимку не удался: {str(e)[:120]}")
        ok = False
    finally:
        if ok:
            shutil.rmtree(aside, ignore_errors=True)
        elif moved:                         # прежний venv отложен нами — на его месте недостроенный новый: убрать и вернуть
            shutil.rmtree(venv, ignore_errors=True)
            try:
                os.replace(aside, venv)
            except OSError as e:
                log.append(f"прежний venv не возвращён: {e}")
        elif not existed:
            shutil.rmtree(venv, ignore_errors=True)
    return ok


def _pip_older_than(vpy, want: tuple) -> bool:
    try:
        p = _run([str(vpy), "-I", "-X", "utf8", "-m", "pip", "--version"], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    m = re.match(r"pip (\d+)\.(\d+)", (p.stdout or "").strip()) if p.returncode == 0 else None
    return bool(m) and (int(m.group(1)), int(m.group(2))) < want


def _upgrade_pip(vpy, days: float, log: list) -> None:
    """pip в venv — до выпуска старше `days` суток. Сбой не мешает установке: pip просто остаётся прежним."""
    try:
        want = aged_version("pip", days) if days else ""
        if days and not want:
            log.append("pip не обновлён: выпуска старше паузы PyPI не назвал")
            return
        _run([str(vpy), "-I", "-X", "utf8", "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
              *([f"pip=={want}"] if want else ["--upgrade", "pip"])], timeout=600)
    except Exception as e:                                    # noqa: BLE001 — сеть, JSON PyPI, pip
        log.append(f"pip не обновлён: {str(e)[:120]}")


def install(extra_id: str, quarantine_days=None) -> dict:
    """Поставить или обновить надстройку в её venv. → {ok, version, log}.

    `quarantine_days`: сколько суток выпуск должен пролежать на PyPI (по умолчанию `QUARANTINE_DAYS`);
    0 — без паузы (проверка свежих версий в CI), набор всё равно ставится по хешам.

    Одна установка на надстройку за раз: две делили бы один venv и одно имя отложенного каталога."""
    from aurora_common import lock_release, lock_take
    guard = LOCKS / f"{extra_id}.install.lock"
    taken, who = lock_take(guard, {"what": "extras-install"}, stale_s=3 * 3600)
    if not taken:
        return {"ok": False, "version": installed_version(extra_id),
                "log": f"установка {EXTRAS[extra_id]['title']} уже идёт (процесс {who.get('pid', '?')}) — дождитесь её конца"}
    try:
        return _install(extra_id, quarantine_days)
    finally:
        lock_release(guard)


def _install(extra_id: str, quarantine_days=None) -> dict:
    ex = EXTRAS[extra_id]
    days = QUARANTINE_DAYS if quarantine_days is None else float(quarantine_days)
    log = []
    vpy = venv_python(extra_id)
    if os.path.isfile(vpy):
        p = _run([str(vpy), "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        have = tuple(int(x) for x in (p.stdout.strip() or "0.0").split("."))
        if have < ex["min_python"]:
            log.append(f"venv на Python {p.stdout.strip()} — пересоздаю: нужен "
                       f"{'.'.join(map(str, ex['min_python']))}+")
            shutil.rmtree(ex["venv"], ignore_errors=True)
    if not os.path.isfile(vpy):
        base = python_for(extra_id)
        if not base:
            need = ".".join(map(str, ex["min_python"]))
            from aurora_common import python_install_hint
            return {"ok": False, "version": "", "log": f"нет Python {need}+ — поставьте его "
                    f"({python_install_hint()}) и нажмите снова"}
        log.append(f"создаю venv {ex['venv']} на {base}")
        ex["venv"].parent.mkdir(parents=True, exist_ok=True)
        p = _run([base, "-I", "-X", "utf8", "-m", "venv", str(ex["venv"])], timeout=300)
        if p.returncode != 0:
            return {"ok": False, "version": "", "log": "\n".join(log + [p.stderr[-400:]])}
    before = installed_version(extra_id)
    # Снимок того, что стоит сейчас: если снимков ещё не было (venv ставили прежние выпуски), откат
    # по нему вернёт venv целиком, а не один верхний пакет.
    if before and not lock_files(extra_id):
        frozen = freeze_text(vpy)
        if frozen:
            save_lock(extra_id, frozen, "before")
    previous = (lock_files(extra_id) or [None])[0]
    if previous is not None and before and _lock_version(previous, ex["dist"]) not in ("", before):
        previous = None         # venv правили мимо движка: последний снимок не про ту версию, что стоит сейчас
    if _pip_older_than(vpy, (22, 2)):            # `--report` появился в pip 22.2
        _upgrade_pip(vpy, days, log)
    chosen, last_ok = "", False
    for spec in ex["pip"]:
        log.append(f"pip install {spec}" + (f" (выпуски моложе {days:g} сут. не берём)" if days else ""))
        text, why = plan_lock(vpy, spec, days, log=log)
        if not text:
            log.append(why)
            continue
        work = tempfile.mkdtemp(prefix="aurora-extras-")
        try:
            lock_path = Path(work) / "lock.txt"
            lock_path.write_text(text, encoding="utf-8")
            last = _install_lock(vpy, lock_path)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if last.returncode == 0:
            chosen, last_ok = text, True
            break
        log.append((last.stderr or last.stdout)[-300:])
    version = installed_version(extra_id)
    ok = bool(version) and last_ok
    res = {"ok": ok, "version": version}
    if ok:
        _upgrade_pip(vpy, days, log)
    # Новая версия Pydantic AI работает, только если прошла проверку совместимости с
    # Авророй. Не прошла — возвращаем ту, что стояла: обновление не должно ломать работу.
    if ok and extra_id == "pydantic-ai":
        chk = check_compat(version)
        res["compat"] = chk
        if not chk.get("ok"):
            why = "; ".join((chk.get("problems") or ["причина не названа"])[:2])
            log.append(f"⚠️ {ex['title']} {version} не прошёл проверку совместимости: {why}")
            if before and before != version:
                log.append(f"возвращаю {before}")
                if previous is not None and restore(extra_id, previous, log):
                    log.append(f"venv возвращён целиком по снимку {previous.name}")
                else:       # снимка нет или откат по нему не вышел — как прежде, одним верхним пакетом
                    _run([str(vpy), "-I", "-X", "utf8", "-m", "pip", "install", "--quiet", f"{ex['dist']}=={before}"],
                         timeout=1800)
                back = installed_version(extra_id)
                res["compat"] = check_compat(back) if back else chk
                res.update(version=back, rolled_back=version)
                log.append(f"оставлена {back}: обновитесь, когда кит научится новой версии")
            ok = res["ok"] = False
            res["error"] = (f"{ex['title']} {version} не совместим с Авророй ({why})"
                            + (f" — оставлена {res['version']}" if res.get("rolled_back") else ""))
            chosen = ""         # снимок несовместимой установки не сохраняем
    if ok and chosen:
        try:
            res["lock"] = save_lock(extra_id, chosen).name
        except OSError as e:
            log.append(f"снимок установки не сохранён: {e}")
    if ok:          # кто что поставил — в общую папку машины: другой пользователь увидит, у кого стоит
        said = record_install(extra_id, res["version"], ex["venv"])
        if said:
            log.append(said)
    skipped = next((x for x in log if x.startswith("⚠️ пауза зависимостей не применена")), "")
    if skipped:         # пауза мягкая: установка прошла без неё, и об этом сказано в итоге, а не только в середине журнала
        res["pause_skipped"] = skipped.partition(": ")[2]
    log.append((f"✅ {ex['title']} {res['version']}" + (" — пауза зависимостей не применена (причина выше)" if skipped else ""))
               if ok else ("" if res.get("error") else "установка не удалась"))
    if ok and extra_id == "graphify":
        why = register_graph_mcp()
        res["mcp"] = not why
        log.append("MCP-сервер графа базы подключён к агенту Авроры" if not why
                   else f"MCP-сервер графа не подключён: {why}")
    res["log"] = "\n".join(log)[-2000:]
    return res


def record_install(extra_id: str, version: str, path) -> str:
    """Запись «я поставил» в общую папку машины (`access_check.record_install`). → пусто или строка журнала."""
    try:
        import access_check as AC
        return AC.record_install(extra_id, version, path)
    except Exception as e:  # noqa: BLE001 — запись — сведение: установку она не роняет
        return f"запись об установке не сохранена: {type(e).__name__}"


# Настоящий запуск graphify так, как его зовёт движок (`agents/graphify_adapter.py`): без `-I`, в окружении venv,
# во временной папке. Тексты — с названием надстройки; перевод — в каталоге сообщений панели.
_RUN_PROBE = "import graphify, networkx; from importlib.metadata import version; print(version({dist!r}))"


def run_probe(extra_id: str, timeout: int = 120) -> dict:
    """Поднимается ли надстройка для этого пользователя → {ok, version, kind, sub, fix, why}.
    Pydantic AI проверяет `agent_core.adapter_probe` (его зовёт прогон иначе — процессом адаптера).
    Причины — словарями прямо здесь: их перевод сверяет `kit:i18n --check` (`ENGINE_REASONS`)."""
    ex = EXTRAS[extra_id]
    title, vpy = ex["title"], venv_python(extra_id)
    no = {"ok": False, "version": "", "sub": "", "fix": "install"}
    try:
        import access_check as AC
        base = AC.venv_base_problem(ex["venv"])
    except Exception:  # noqa: BLE001
        base = ""
    foreign = {**no, "kind": "venv_foreign_base",
               "why": f"venv {title} построен на Python другого пользователя или на Python, которого больше нет, — "
                      f"для вас он не запустится: поставьте для себя: «Установка» → {title}"}
    if base in ("missing", "denied"):
        return foreign
    if not os.path.isfile(vpy):
        return {**no, "kind": "venv", "why": f"venv {title} не найден — «Установка» → {title}"}
    scratch = tempfile.mkdtemp(prefix="aurora-probe-")
    try:
        p = subprocess.run([str(vpy), "-c", _RUN_PROBE.format(dist=ex["dist"])], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=scratch,
                           env=venv_env({k: v for k, v in os.environ.items() if not k.startswith("Malloc")},
                                        GRAPHIFY_OUT=os.path.join(scratch, "graphify-out")))
    except subprocess.TimeoutExpired:
        return {**no, "kind": "process_hang", "why": f"процесс {title} не ответил за {timeout} с — «Установка» → {title}"}
    except OSError as e:
        return foreign if base else {**no, "kind": "process_start",
                                     "why": f"процесс {title} не запускается ({type(e).__name__}) — «Установка» → {title}"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if p.returncode == 0:
        return {"ok": True, "version": (p.stdout.strip().splitlines() or [""])[-1], "kind": "", "sub": "",
                "fix": "", "why": ""}
    exc = re.findall(r"^(\w+(?:Error|Exception))\b", p.stderr or "", re.M)
    if exc:
        return {**no, "kind": "import_failed",
                "why": f"{title} не импортируется в своём venv ({exc[-1]}) — «Установка» → {title}, переустановить"}
    return foreign if base else {**no, "kind": "process_died",
                                 "why": f"процесс {title} завершился сразу при запуске (код {p.returncode}) — "
                                        f"«Установка» → {title}"}


GRAPH_MCP = "aurora-graph"
GRAPH_REL = os.path.join("AuroraKnowledgeDB", "meta", "graphify", "graph.json")
GRAPH_OUT = os.path.join("AuroraKnowledgeDB", "meta", "graphify")


def graph_entry(vpy) -> dict:
    """Запись сервера графа в `local/mcp.json`.

    `GRAPHIFY_OUT` — папка выгрузки: инструменты graphify принимают `project_path` и ищут
    граф в `<project_path>/graphify-out/`, а наш лежит в `AuroraKnowledgeDB/meta/graphify/`.
    Встроенный агент `project_path` не передаёт вовсе (`drop_args`): модель подставляла
    выдуманный путь («/app/…») и перебирала варианты до потолка вызовов — граф так ни разу и
    не открылся (PRJ-A, 28.09.2026). Сервер и так запущен в папке проекта.

    `-I`: питон запускается в папке проекта, и без него эта папка попадала бы в `sys.path`
    первой — каталог `graphify/` из репозитория перекрыл бы установленный модуль, а исполнился
    бы код из git с токенами и окружением сервера (BL-SEC-12).
    """
    return {"command": str(vpy), "args": ["-I", "-m", "graphify.serve", GRAPH_REL],
            "env": {"GRAPHIFY_OUT": GRAPH_OUT},
            "about": "граф базы знаний проекта: связи карточек, темы, кратчайший путь между "
                     "понятиями, самые связанные карточки, соседи карточки",
            "drop_args": ["project_path"]}
KIT = Path(__file__).resolve().parent.parent


def _kit_dir() -> Path:
    """Кит: эта папка либо (в копии движка проекта) та, на которую указывает `kit_path.txt`. Битый указатель — эта папка."""
    ptr = KIT / "kit_path.txt"
    if KIT.name in (".aurora", ".opencode") and ptr.is_file():       # движок проекта; .opencode — до 1.158
        try:
            text = ptr.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return KIT
        # пустой указатель дал бы `Path("")` — текущую папку, то есть проект; несуществующий путь — несуществующий кит
        return Path(text) if text and Path(text).is_dir() else KIT
    return KIT


def kit_mcp_file() -> Path:
    """Серверы MCP машины (`<кит>/local/mcp.json`, с 1.127.0). В копии движка проекта —
    кит по `kit_path.txt`."""
    return _kit_dir() / "local" / "mcp.json"


def requirements_pins(path=None) -> dict:
    """{пакет: версия} из закреплённых строк `requirements.txt` кита (`имя==версия`), имена в нижнем регистре.

    Незакреплённые строки и комментарии пропускаются. Файла нет — пусто: закрепление не выдумывают."""
    if path is None:
        path = _kit_dir() / "requirements.txt"
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return {}
    pins = {}
    for ln in lines:
        m = re.match(r"^([A-Za-z0-9][\w.\-]*)==([\w.]+)\s*(?:#.*)?$", ln.strip())
        if m:
            pins[re.sub(r"[-_.]+", "-", m.group(1)).casefold()] = m.group(2)
    return pins


def pinned_spec(pkg: str) -> str:
    """`пакет==версия`, если кит закрепил этот пакет, иначе пусто."""
    ver = requirements_pins().get(re.sub(r"[-_.]+", "-", pkg).casefold())
    return f"{pkg}=={ver}" if ver else ""


def safe_spec(pkg: str, days=None) -> tuple:
    """Что ставить в Python панели для пакета из списка «Установки». → (спецификация, пояснение).

    Закреплённое китом — как закреплено; остальное — самый новый выпуск старше `QUARANTINE_DAYS` суток.
    Не получилось спросить PyPI — пакет без версии и пояснение об этом: необязательный пакет не должен
    перестать ставиться из-за сети, но молчать о том, что паузы не было, нельзя."""
    pin = pinned_spec(pkg)
    if pin:
        return pin, ""
    days = QUARANTINE_DAYS if days is None else days
    try:
        ver = aged_version(pkg, days) if days else ""
    except Exception as e:                                    # noqa: BLE001 — сеть, формат ответа PyPI
        return pkg, f"паузу после выпуска проверить не удалось ({str(e)[:80]}) — ставлю последнюю версию"
    return (f"{pkg}<={ver}" if ver else pkg), ("" if ver or not days else "выпуска старше паузы не нашлось — ставлю последнюю")


def graph_mcp() -> dict:
    """{registered, name, command, args} — MCP-сервер графа базы, если graphify стоит."""
    vpy = venv_python("graphify")
    if not os.path.isfile(vpy):
        return {}
    try:
        servers = json.loads(kit_mcp_file().read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        servers = {}
    want = graph_entry(vpy)
    return {"registered": servers.get(GRAPH_MCP) == want, "name": GRAPH_MCP,
            "command": want["command"], "args": want["args"], "env": want["env"]}


def register_graph_mcp() -> str:
    """MCP-сервер графа базы — в серверы машины рядом с чужими. → пусто или причина.

    Путь к графу относительный: сервер запускается из папки проекта, и один сервер
    отвечает за тот проект, где работает агент. Машинный путь к питону живёт в `local/`
    — в git он не уходит. Прежний файл остаётся рядом как `.bak`, оба — только владельцу.
    """
    vpy = venv_python("graphify")
    if not os.path.isfile(vpy):
        return "graphify не установлен"
    path = kit_mcp_file()
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as e:
            return f"local/mcp.json не разобран: {e}"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    servers = servers if isinstance(servers, dict) else {}
    want = graph_entry(vpy)
    if servers.get(GRAPH_MCP) == want:
        return ""
    servers[GRAPH_MCP] = want
    data = dict(data if isinstance(data, dict) else {}, mcpServers=servers)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            bak = path.with_name(path.name + ".bak")
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(bak, 0o600)
        tmp = path.with_name(path.name + ".aurora-new")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        replace_file(tmp, path)
    except OSError as e:
        return f"не удалось записать local/mcp.json: {e}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Надстройки движка: Pydantic AI и graphify")
    ap.add_argument("--status", action="store_true", help="что стоит и что вышло")
    ap.add_argument("--fresh", action="store_true", help="спросить GitHub и PyPI заново")
    ap.add_argument("--offline", action="store_true", help="без сети: только что стоит")
    ap.add_argument("--json", action="store_true", help="машиночитаемо")
    ap.add_argument("--install", choices=sorted(EXTRAS), metavar="ID",
                    help="поставить или обновить: " + ", ".join(sorted(EXTRAS)))
    ap.add_argument("--no-quarantine", action="store_true",
                    help="с --install: без паузы после выпуска (по умолчанию семь суток); хеши остаются")
    a = ap.parse_args()
    if a.install:
        res = install(a.install, 0 if a.no_quarantine else None)
        print(json.dumps(res, ensure_ascii=False) if a.json else res["log"])
        return 0 if res["ok"] else 1
    rows = status(a.fresh, online=not a.offline)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for r in rows:
        state = f"стоит {r['installed']}" if r["installed"] else "не установлен"
        print(f"{r['title']}: {state} · в git: {r['git'] or '—'} · на PyPI: {r['pypi'] or '—'}"
              + (" · можно обновить" if r["update"] else ""))
        print(f"  {r['enables']}")
        if r["error"]:
            print(f"  ⚠️ {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
