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

Версия «в git» — последний выпуск репозитория надстройки на GitHub: так пользователь
видит, что вышло. Ставится она через pip с PyPI; если PyPI от git отстаёт, статус
называет обе версии честно.

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
import time
import urllib.request
from pathlib import Path

HOME = Path.home() / ".aurora"
CACHE = HOME / "extras-cache.json"
CACHE_TTL = 6 * 3600            # как у проверки версии кита: сеть трогаем редко

EXTRAS = {
    "pydantic-ai": {
        "title": "Pydantic AI",
        "dist": "pydantic-ai",
        "pip": ["pydantic-ai"],
        "venv": HOME / "venv",
        "repo": "pydantic/pydantic-ai",
        "min_python": (3, 9),
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
    env = {k: v for k, v in os.environ.items() if not k.startswith("Malloc")}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def installed_version(extra_id: str) -> str:
    """Версия, стоящая в venv надстройки. Пусто — не стоит."""
    vpy = venv_python(extra_id)
    if not vpy.is_file():
        return ""
    dist = EXTRAS[extra_id]["dist"]
    try:
        p = _run([str(vpy), "-c", "from importlib.metadata import version; "
                  f"print(version({dist!r}))"], timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "aurora-studio",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000).decode("utf-8", "replace"))


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
    out = {"git": "", "pypi": "", "error": "", "at": time.time()}
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
        out["pypi"] = _get_json(f"https://pypi.org/pypi/{ex['dist']}/json")["info"]["version"]
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
        new = latest(eid, fresh) if online else {"git": "", "pypi": "", "error": ""}
        avail = new.get("pypi") or ""
        rows.append({
            "id": eid, "title": ex["title"], "enables": ex["enables"],
            "installed": have, "git": new.get("git", ""), "pypi": avail,
            "error": new.get("error", ""),
            # Обновить можно до того, что ставит pip, — до версии PyPI.
            "update": bool(have and avail and _vt(avail) > _vt(have)),
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
    """Питон, на котором можно поставить надстройку: этот, если подходит, иначе новейший
    найденный. Пусто — подходящего нет (graphify требует 3.10+)."""
    need = EXTRAS[extra_id]["min_python"]
    if sys.version_info[:2] >= need:
        return sys.executable
    names = [f"python3.{m}" for m in range(20, need[1] - 1, -1)]
    dirs = ["", "/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    for n in names:
        for d in dirs:
            path = shutil.which(n) if not d else os.path.join(d, n)
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return ""


def check_compat(version: str) -> dict:
    """Самопроверка адаптера Pydantic AI для этой версии (заново, без памяти)."""
    try:
        import agent_core as AG
    except ImportError:
        return {"ok": True, "problems": [], "version": version}
    return AG.adapter_selfcheck(version, force=True)


def install(extra_id: str) -> dict:
    """Поставить или обновить надстройку в её venv. → {ok, version, log}."""
    ex = EXTRAS[extra_id]
    log = []
    vpy = venv_python(extra_id)
    if vpy.is_file():
        p = _run([str(vpy), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"])
        have = tuple(int(x) for x in (p.stdout.strip() or "0.0").split("."))
        if have < ex["min_python"]:
            log.append(f"venv на Python {p.stdout.strip()} — пересоздаю: нужен "
                       f"{'.'.join(map(str, ex['min_python']))}+")
            shutil.rmtree(ex["venv"], ignore_errors=True)
    if not vpy.is_file():
        base = python_for(extra_id)
        if not base:
            need = ".".join(map(str, ex["min_python"]))
            return {"ok": False, "version": "", "log": f"нет Python {need}+ — поставьте его "
                    f"(например, brew install python) и нажмите снова"}
        log.append(f"создаю venv {ex['venv']} на {base}")
        ex["venv"].parent.mkdir(parents=True, exist_ok=True)
        p = _run([base, "-m", "venv", str(ex["venv"])], timeout=300)
        if p.returncode != 0:
            return {"ok": False, "version": "", "log": "\n".join(log + [p.stderr[-400:]])}
    before = installed_version(extra_id)
    _run([str(vpy), "-m", "pip", "install", "--quiet", "--upgrade", "pip"], timeout=600)
    last = None
    for spec in ex["pip"]:
        log.append(f"pip install --upgrade {spec}")
        last = _run([str(vpy), "-m", "pip", "install", "--upgrade", "--quiet", *spec.split()],
                    timeout=1800)
        if last.returncode == 0:
            break
        log.append((last.stderr or last.stdout)[-300:])
    version = installed_version(extra_id)
    ok = bool(version) and last is not None and last.returncode == 0
    res = {"ok": ok, "version": version}
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
                _run([str(vpy), "-m", "pip", "install", "--quiet", f"{ex['dist']}=={before}"],
                     timeout=1800)
                back = installed_version(extra_id)
                res["compat"] = check_compat(back) if back else chk
                res.update(version=back, rolled_back=version)
                log.append(f"оставлена {back}: обновитесь, когда кит научится новой версии")
            ok = res["ok"] = False
            res["error"] = (f"{ex['title']} {version} не совместим с Авророй ({why})"
                            + (f" — оставлена {res['version']}" if res.get("rolled_back") else ""))
    log.append(f"✅ {ex['title']} {res['version']}" if ok else
               ("" if res.get("error") else "установка не удалась"))
    if ok and extra_id == "graphify":
        why = register_graph_mcp()
        res["mcp"] = not why
        log.append("MCP-сервер графа базы подключён к агенту Авроры" if not why
                   else f"MCP-сервер графа не подключён: {why}")
    res["log"] = "\n".join(log)[-2000:]
    return res


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
    """
    return {"command": str(vpy), "args": ["-m", "graphify.serve", GRAPH_REL],
            "env": {"GRAPHIFY_OUT": GRAPH_OUT},
            "about": "граф базы знаний проекта: связи карточек, темы, кратчайший путь между "
                     "понятиями, самые связанные карточки, соседи карточки",
            "drop_args": ["project_path"]}
KIT = Path(__file__).resolve().parent.parent


def kit_mcp_file() -> Path:
    """Серверы MCP машины (`<кит>/local/mcp.json`, с 1.127.0). В копии движка проекта —
    кит по `kit_path.txt`."""
    kit = KIT
    ptr = KIT / "kit_path.txt"
    if KIT.name == ".opencode" and ptr.is_file():
        kit = Path(ptr.read_text(encoding="utf-8").strip())
    return kit / "local" / "mcp.json"


def graph_mcp() -> dict:
    """{registered, name, command, args} — MCP-сервер графа базы, если graphify стоит."""
    vpy = venv_python("graphify")
    if not vpy.is_file():
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
    if not vpy.is_file():
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
        os.replace(tmp, path)
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
    a = ap.parse_args()
    if a.install:
        res = install(a.install)
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
