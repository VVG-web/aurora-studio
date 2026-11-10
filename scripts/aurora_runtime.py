#!/usr/bin/env python3
"""aurora_runtime.py — единая конфигурация Python для всех подсистем Авроры.

Одна запись `~/.aurora/config.json` отвечает на вопрос «каким Python работает Аврора»:
установщик, проверка совместимости, `agent_core.py`, MCP-серверы, инструменты и дочерние
процессы. Путь к интерпретатору не собирается из версии (`bin/python3.15`): он берётся из
фактического запуска и проверки.

Почему это отдельный модуль:
* `sys.executable` базового Python бывает висячей ссылкой (python.org 3.15 рядом с
  `python3`), и venv, собранный на такой базе, выглядит рабочим только до первого запуска;
* Homebrew Python помечен PEP 668 (`EXTERNALLY-MANAGED`), глобальная установка пакетов в
  него запрещена — Аврора должна сама уйти в venv, а не падать;
* установка, проверка импорта и прогон раньше могли идти разными интерпретаторами.

Модуль намеренно не импортирует `aurora_common`: его читают установщик и проверки, когда
движок ещё не разложен по проекту.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    # Защита версии Python: на старом интерпретаторе человек должен увидеть объяснение,
    # а не трейсбек. В установщике aurora_common может ещё не быть — тогда модуль обязан
    # работать сам (он и создан для первого запуска).
    from aurora_common import require_python  # noqa: F401
except ModuleNotFoundError:
    pass

HOME = Path(os.environ.get("AURORA_HOME") or (Path.home() / ".aurora"))
CONFIG = HOME / "config.json"
DEFAULT_VENV = HOME / "venv"
PYTHON_MIN = (3, 10)
PYTHON_RECOMMENDED = (3, 14)
PYDANTIC_MODULE = "pydantic_ai"
PYDANTIC_PROVIDERS = "pydantic_ai.providers"
CONFIG_VERSION = 1
CURRENT_TTL = 60.0             # секунд: /api/state зовут часто, а запуск проверки — не бесплатный
_PROBE_TIMEOUT = 15.0

# Кандидаты перебираются в этом порядке: сначала текущий процесс, затем установки, которые
# человек поставил сам, и только потом системный Python. Системный на macOS часто старый.
SOURCE_ORDER = ("python.org", "homebrew", "store", "system", "custom", "venv")
# Схема sysconfig у разных сборок разная: MacPorts 3.13 не знает ключа `usersite`
# (`sysconfig.get_path('usersite')` бросает KeyError), и вся проба считалась неудачной —
# годный интерпретатор объявлялся нерабочим. Поэтому каждое поле читается отдельно:
# сбой одного поля не отменяет ответ остальных.
PROBE = (
    "import json, os, sys, sysconfig\n"
    "def _safe(fn, default=None):\n"
    " try:\n"
    "  return fn()\n"
    " except Exception:\n"
    "  return default\n"
    "paths = _safe(sysconfig.get_paths, {}) or {}\n"
    "purelib = paths.get('purelib') or ''\n"
    "site_dir = _safe(lambda: sysconfig.get_path('usersite'), '') or ''\n"
    "if not site_dir:\n"
    " try:\n"
    "  import site as _site\n"
    "  site_dir = _site.getusersitepackages() or ''\n"
    " except Exception:\n"
    "  site_dir = ''\n"
    "stdlib = _safe(lambda: sysconfig.get_path('stdlib'), '') or ''\n"
    "print(json.dumps({\n"
    " 'executable': sys.executable,\n"
    " 'real': os.path.realpath(sys.executable),\n"
    " 'version': '%d.%d.%d' % sys.version_info[:3],\n"
    " 'prefix': sys.prefix,\n"
    " 'base_prefix': sys.base_prefix,\n"
    " 'venv': sys.prefix != sys.base_prefix,\n"
    " 'writable': os.access(purelib, os.W_OK),\n"
    " 'purelib': purelib,\n"
    " 'user_site': site_dir,\n"
    " 'externally_managed': bool(stdlib) and os.path.isfile(\n"
    "     os.path.join(stdlib, 'EXTERNALLY-MANAGED')),\n"
    "}))\n"
)
PYDANTIC_PROBE = (
    "import json\n"
    "try:\n"
    " import pydantic_ai\n"
    " import pydantic_ai.providers\n"
    " from pydantic_ai.providers.openai import OpenAIProvider\n"
    " print(json.dumps({'ok': True, 'version': getattr(pydantic_ai, '__version__', ''),\n"
    "  'module': pydantic_ai.__file__, 'providers': pydantic_ai.providers.__file__}))\n"
    "except Exception as e:\n"
    " print(json.dumps({'ok': False, 'error': type(e).__name__ + ': ' + str(e),\n"
    "  'module': getattr(e, 'name', '')}))\n"
)
_CURRENT: dict = {"at": 0.0, "stamp": None, "result": None}


class RuntimeError_(RuntimeError):
    """Конфигурация есть, но она не проходит проверку. Тихий откат запрещён."""


def _env() -> dict:
    """Окружение проверки: чужой PYTHONHOME/PYTHONPATH не должен влиять на выбор Python."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("PYTHONHOME", "PYTHONPATH")}
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run(path, args, timeout: float = _PROBE_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run([str(path), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          env=_env())


def _probe_path(path) -> str:
    """Фактический путь, который называет сам интерпретатор. Пусто — не запустился."""
    try:
        p = _run(path, ["-I", "-c", PROBE])
    except (OSError, subprocess.SubprocessError):
        return ""
    if p.returncode != 0:
        return ""
    line = (p.stdout or "").strip().splitlines()[-1:] or [""]
    return line[0]


def _parse_probe(text: str) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _version_tuple(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(text or ""))[:3]) or (0,)


def _version_level(version) -> str:
    vi = _version_tuple(version)[:2]
    if vi >= PYTHON_MIN:
        return "ok"
    return "error"


def detect_source(path: str) -> str:
    """Тип установки по пути. Это подсказка человеку, а не способ угадать интерпретатор."""
    p = str(path or "").replace("\\", "/").casefold()
    if "/.aurora/" in p and ("/venv/" in p or "/venv-" in p or "/graphify/" in p):
        return "venv"
    if "/windowsapps/" in p:
        return "store"
    if ("/library/frameworks/python.framework/" in p or "/programs/python/python" in p
            or "/program files/python" in p or "/appdata/local/programs/python/" in p):
        return "python.org"
    if "/opt/homebrew/" in p or "/usr/local/cellar/" in p or "/homebrew/" in p:
        return "homebrew"
    if p in ("/usr/bin/python3", "/usr/bin/python") or "/system/library/frameworks/" in p:
        return "system"
    return "custom"


def validate_python(path, timeout: float = _PROBE_TIMEOUT) -> dict:
    """Проверить интерпретатор фактическим запуском. → описание, годное или нет.

    `base_python -c "import sys; print(sys.executable)"` может напечатать несуществующий
    файл (так python.org 3.15.2 оставляет `python3.15` рядом с `python3`). Поэтому
    проверяем и напечатанный путь, и сам запуск. Невалидная база не должна создавать venv.
    """
    raw = str(path or "").strip()
    out = {"path": raw, "executable": "", "real": "", "version": "", "source": detect_source(raw),
           "externally_managed": False, "venv": False, "valid": False, "reason": "",
           "writable": False, "purelib": "", "user_site": "", "broken_link": False, "level": "error"}
    if not raw:
        out["reason"] = "путь к Python не задан"
        return out
    if os.path.islink(raw) and not os.path.exists(raw):
        out["broken_link"] = True
        out["reason"] = "висячая ссылка: файла, на который она указывает, нет"
        return out
    if not os.path.isfile(raw):
        out["reason"] = "файла нет или это не файл"
        return out
    if not os.access(raw, os.X_OK):
        out["reason"] = "файл не исполняемый"
        return out
    text = _probe_path(raw)
    data = _parse_probe(text)
    if not data:
        out["reason"] = "интерпретатор не запустился или не ответил"
        return out
    exe = str(data.get("executable") or "")
    real = str(data.get("real") or "")
    out.update({
        "executable": exe, "real": real,
        "version": str(data.get("version") or ""),
        "externally_managed": bool(data.get("externally_managed")),
        "venv": bool(data.get("venv")),
        "writable": bool(data.get("writable")),
        "purelib": str(data.get("purelib") or ""),
        "user_site": str(data.get("user_site") or ""),
    })
    out["source"] = detect_source(exe or raw)
    out["level"] = _version_level(out["version"])
    # Главная проверка: sys.executable, названный базой, должен существовать. У python.org
    # 3.15.2 он может указывать на отсутствующий python3.15, хотя процесс запущен через python3.
    if not exe or not os.path.isfile(exe):
        out["reason"] = (f"база называет себя {exe or 'без пути'}, но этого файла нет: "
                         "интерпретатор неконсистентен")
        return out
    if not os.access(exe, os.X_OK):
        out["reason"] = f"база называет себя {exe}, но файл не исполняемый"
        return out
    if out["level"] != "ok":
        out["reason"] = f"Python {out['version'] or '?'} старше {'.'.join(map(str, PYTHON_MIN))}"
        return out
    out["valid"] = True
    return out


def _known_paths() -> list:
    """Пути установок, которые не всегда попадают в PATH (особенно на Windows и macOS)."""
    names = {"darwin": ("python3", "python3.10", "python3.11", "python3.12", "python3.13",
                        "python3.14", "python3.15", "python3.16"),
             "nt": ("python.exe", "python3.exe"),
             "linux": ("python3", "python3.10", "python3.11", "python3.12", "python3.13",
                       "python3.14", "python3.15", "python3.16")}
    plat = "darwin" if sys.platform == "darwin" else "nt" if os.name == "nt" else "linux"
    out = []
    for name in names[plat]:
        for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
            out.append(os.path.join(d, name))
    # python.org framework: версия лежит в отдельной папке.
    for root in ("/Library/Frameworks/Python.framework/Versions",
                 str(Path.home() / "Library/Frameworks/Python.framework/Versions")):
        try:
            for v in sorted(Path(root).glob("*/bin"), reverse=True):
                out.extend(str(v / n) for n in ("python3", "python"))
        except OSError:
            pass
    if plat == "nt":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)")):
            if not base:
                continue
            try:
                for d in (Path(base) / "Programs/Python").glob("Python*"):
                    out.append(str(d / "python.exe"))
                for d in Path(base).glob("Python*"):
                    out.append(str(d / "python.exe"))
            except OSError:
                pass
        # Microsoft Store ставит исполняемые заглушки в WindowsApps; настоящий интерпретатор
        # находится по ссылке, её проверка всё равно скажет, что запустилось.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            try:
                for d in (Path(local) / "Microsoft/WindowsApps").glob("python*.exe"):
                    out.append(str(d))
            except OSError:
                pass
    return out


def _py_launcher_paths() -> list:
    """Windows `py -0p`: строки вида `-V:3.12 * C:\\...\\python.exe`."""
    if os.name != "nt" or not shutil.which("py"):
        return []
    try:
        p = subprocess.run(["py", "-0p"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10, env=_env())
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (p.stdout or "").splitlines():
        m = re.search(r"([A-Za-z]:\\.*?" + re.escape("python.exe") + r")", line, re.I)
        if m:
            out.append(m.group(1).strip())
    return out


def _path_names() -> list:
    plat = "nt" if os.name == "nt" else "darwin" if sys.platform == "darwin" else "linux"
    names = (("python3", "python3.15", "python3.14", "python3.13", "python3.12",
              "python3.11", "python3.10", "python") if plat == "nt" else
             ("python3", "python3.15", "python3.14", "python3.13", "python3.12",
              "python3.11", "python3.10"))
    out = []
    for n in names:
        p = shutil.which(n)
        if p:
            out.append(p)
    return out


def _existing_venvs() -> list:
    out = []
    try:
        for d in HOME.glob("*/"):
            for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
                p = d / rel
                if os.path.lexists(p):
                    out.append(str(p))
    except OSError:
        pass
    return out


def candidates(include_invalid: bool = True) -> dict:
    """Все найденные интерпретаторы: текущий, PATH, известные каталоги, py-лаунчер, venv.

    → {"valid": [...], "invalid": [...], "paths": [...]}. Пути уникальны по realpath,
    висячие ссылки остаются в invalid с причиной: ими нельзя создавать venv.
    """
    raw = [sys.executable, *_path_names(), *_known_paths(), *_py_launcher_paths(),
           *_existing_venvs()]
    seen, valid, invalid = set(), [], []
    for p in raw:
        if not p:
            continue
        try:
            key = os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(p)))))
        except OSError:
            key = os.path.normcase(os.path.abspath(os.path.expanduser(str(p))))
        if key in seen:
            continue
        seen.add(key)
        row = validate_python(p)
        (valid if row["valid"] else invalid).append(row)

    def rank(row):
        src = row.get("source") or "custom"
        return (SOURCE_ORDER.index(src) if src in SOURCE_ORDER else 99,
                tuple(-x for x in _version_tuple(row.get("version"))))
    valid.sort(key=rank)
    invalid.sort(key=lambda r: r.get("path") or "")
    return {"valid": valid, "invalid": invalid if include_invalid else []}


def best(source: str = "", path: str = "") -> dict:
    """Лучший валидный интерпретатор. `source`/`path` — явный выбор человека."""
    if path:
        found = validate_python(path)
        return found if found.get("valid") else {}
    found = candidates(include_invalid=False)["valid"]
    if source:
        found = [x for x in found if x["source"] == source]
    return found[0] if found else {}


def venv_path() -> Path:
    return DEFAULT_VENV


def venv_candidates(path) -> list:
    """Кандидаты на интерпретатор venv. Не собираем `python3.<minor>` из версии базы:
    сначала фактические файлы, затем проверка каждого запуском."""
    root = Path(path)
    if os.name == "nt":
        names = ("Scripts/python.exe", "Scripts/python3.exe", "Scripts/pythonw.exe")
    else:
        names = ("bin/python", "bin/python3")
        # Файлы `python3.X` читаем из каталога, а не угадываем номер версии.
        try:
            names += tuple("bin/" + p.name for p in sorted((root / "bin").glob("python3.*")))
        except OSError:
            pass
    out = []
    for name in names:
        p = root / name
        if os.path.lexists(p):
            out.append(str(p))
    return out


def find_venv_python(path) -> Path | None:
    """Первый реально работающий Python venv; путь не строится из версии.

    Ровно здесь ломался python.org 3.15.2: `bin/python3.15` — висячая ссылка, а
    `bin/python`/`bin/python3` могут быть живыми. Проверяем запуском, а не именем.
    """
    for p in venv_candidates(path):
        try:
            if not os.path.isfile(p) or not os.access(p, os.X_OK):
                continue
            got = validate_python(p)
        except (OSError, subprocess.SubprocessError):
            continue
        if got.get("valid") or got.get("version"):
            # venv может быть построен на Python со статусом warn (3.10), но запускаться
            # обязан. Валидность базы и пригодность venv — разные проверки.
            if got.get("executable") and os.path.isfile(got["executable"]):
                return Path(p)
    return None


def create_venv(base: str, path=None, timeout: float = 300) -> dict:
    """Создать venv только из проверенной базы и вернуть фактический effective_python.

    При сбое каталог удаляется, чтобы не оставался «полурабочий» venv. Если в готовом venv
    нет ни одного живого интерпретатора, это тоже сбой: повреждённые ссылки не считаем
    успехом.
    """
    target = Path(path or venv_path())
    checked = validate_python(base)
    if not checked["valid"]:
        return {"ok": False, "error": f"база Python не прошла проверку: {checked['reason']}",
                "base": checked, "venv": str(target), "effective": "", "links": []}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        p = _run(checked["executable"], ["-I", "-X", "utf8", "-m", "venv", str(target)],
                 timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        shutil.rmtree(target, ignore_errors=True)
        return {"ok": False, "error": f"venv не создан: {type(e).__name__}: {e}",
                "base": checked, "venv": str(target), "effective": "", "links": []}
    if p.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        return {"ok": False, "error": (p.stderr or p.stdout or "venv завершился с ошибкой")[-1000:],
                "base": checked, "venv": str(target), "effective": "", "links": []}
    eff = find_venv_python(target)
    if not eff:
        links = []
        for name in venv_candidates(target):
            links.append({"path": name, "target": os.path.realpath(name),
                          "exists": os.path.exists(name)})
        shutil.rmtree(target, ignore_errors=True)
        return {"ok": False, "error": "venv создан, но ни один его Python не запускается: "
                "висячие ссылки или неконсистентная база",
                "base": checked, "venv": str(target), "effective": "", "links": links}
    return {"ok": True, "error": "", "base": checked, "venv": str(target),
            "effective": str(eff), "links": []}


def read_config() -> dict:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(data: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_name(CONFIG.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, CONFIG)


def _runtime(cfg: dict) -> dict:
    rt = cfg.get("runtime") if isinstance(cfg, dict) else {}
    return rt if isinstance(rt, dict) else {}


def _config_stamp() -> tuple:
    try:
        st = CONFIG.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _drop_cache() -> None:
    _CURRENT["at"] = 0.0
    _CURRENT["stamp"] = None
    _CURRENT["result"] = None


def pydantic_info(python: str = "") -> dict:
    """Проверить `pydantic_ai.providers` и версию в конкретном интерпретаторе."""
    exe = python or (current().get("effective_python") or sys.executable)
    try:
        p = _run(exe, ["-I", "-c", PYDANTIC_PROBE], timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "version": "", "module": ""}
    try:
        data = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (p.stderr or "проверка не ответила")[-500:],
                "version": "", "module": ""}
    return {"ok": bool(data.get("ok")), "version": str(data.get("version") or ""),
            "error": str(data.get("error") or ""), "module": str(data.get("module") or ""),
            "providers": str(data.get("providers") or "")}


def _python_for_extra(version: str) -> str:
    """Интерпретатор, который действительно запускается и годится по версии."""
    checked = validate_python(version)
    return checked["executable"] if checked.get("valid") else ""


def configure(source: str = "", base_python: str = "", isolated=None,
              path=None, check_pydantic: bool = False, write: bool = True) -> dict:
    """Выбрать и сохранить runtime. Создание venv — часть выбора, а не отдельная догадка.

    Homebrew и PEP 668 принудительно переводятся в venv: глобальная установка в них
    запрещена. Для остальных `isolated` задаёт человек, по умолчанию — True (единый
    интерпретатор для всех подсистем).
    """
    found = best(source, base_python)
    if not found or not found.get("valid"):
        return {"ok": False, "error": "подходящий Python не найден", "candidates": candidates()}
    src = found.get("source") or detect_source(found["executable"])
    auto_isolated = src == "homebrew" or bool(found.get("externally_managed"))
    isolated = True if auto_isolated else (True if isolated is None else bool(isolated))
    target = Path(path or venv_path())
    effective = found["executable"]
    if isolated:
        made = create_venv(found["executable"], target)
        if not made["ok"]:
            return {"ok": False, "error": made["error"], "base": found, "venv": str(target),
                    "links": made.get("links") or [], "candidates": candidates()}
        effective = made["effective"]
    cfg = read_config()
    runtime = {
        "source": src,
        "base_python": found["executable"],
        "venv_path": str(target) if isolated else "",
        "effective_python": effective,
        "python_version": found.get("version", ""),
        "isolated": isolated,
        "externally_managed": bool(found.get("externally_managed")),
        "validated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pydantic_ai_version": "",
    }
    if check_pydantic:
        info = pydantic_info(effective)
        runtime["pydantic_ai_version"] = info.get("version", "") if info.get("ok") else ""
    if write:
        cfg["version"] = CONFIG_VERSION
        cfg["runtime"] = runtime
        _write_config(cfg)
        _drop_cache()
    return {"ok": True, "runtime": runtime, "base": found}


def current(force: bool = False) -> dict:
    """Прочитать и перепроверить runtime при каждом старте. Никакого тихого fallback."""
    stamp = _config_stamp()
    if not force and _CURRENT["result"] is not None and _CURRENT["stamp"] == stamp \
            and time.time() - _CURRENT["at"] < CURRENT_TTL:
        return _CURRENT["result"]
    cfg = read_config()
    rt = _runtime(cfg)
    if not rt:
        # Текущий процесс уже запущен: его версия и путь известны без дочернего запуска.
        # Проба здесь стоила секунды на каждом старте панели без конфигурации.
        ok = (tuple(sys.version_info[:2]) >= PYTHON_MIN and bool(sys.executable)
              and os.path.isfile(sys.executable) and os.access(sys.executable, os.X_OK))
        result = {"configured": False, "ok": False, "reason": "runtime ещё не выбран",
                  "effective_python": "", "runtime": {}, "base": {},
                  "effective": {}, "pydantic": {},
                  "bootstrap": {"valid": ok, "executable": sys.executable,
                                "version": "%d.%d.%d" % sys.version_info[:3],
                                "reason": "" if ok else "текущий Python не годится"}}
        _CURRENT.update({"at": time.time(), "stamp": stamp, "result": result})
        return result
    base = validate_python(rt.get("base_python") or "")
    eff = validate_python(rt.get("effective_python") or "")
    reason = ""
    if not base.get("valid"):
        reason = f"базовый Python не проходит проверку: {base.get('reason')}"
    elif not eff.get("valid"):
        reason = f"effective_python не проходит проверку: {eff.get('reason')}"
    elif rt.get("isolated") and not eff.get("venv"):
        reason = "в конфигурации выбран venv, но effective_python не является venv"
    pyd = pydantic_info(eff["executable"]) if eff.get("valid") else {"ok": False, "error": reason}
    result = {"configured": True, "ok": not reason, "reason": reason,
              "runtime": rt, "base": base, "effective": eff,
              "effective_python": eff.get("executable") or "",
              "pydantic": pyd}
    _CURRENT.update({"at": time.time(), "stamp": stamp, "result": result})
    return result


def effective_python() -> str:
    """Единственный интерпретатор для подсистем. Нет валидной конфигурации — ошибка.

    До первого выбора конфигурации разрешён текущий Python (иначе движок не смог бы
    показать экран выбора), но после записи runtime несоответствие не лечится молча.
    """
    got = current()
    if got.get("ok"):
        return got["effective_python"]
    if not got.get("configured") and got.get("bootstrap", {}).get("valid"):
        return sys.executable
    raise RuntimeError_(got.get("reason") or "runtime Python не настроен")


def python_or_self() -> str:
    """Интерпретатор подсистемы без исключения: нет валидной конфигурации — текущий процесс.

    Так ведут себя места, которые обязаны подняться даже до выбора runtime (панель,
    установщик, справка). Рабочие подсистемы зовут `effective_python()` и падают явно.
    """
    try:
        return effective_python()
    except RuntimeError_:
        return sys.executable


def child_python() -> str:
    """Интерпретатор для дочернего процесса: тот же effective_python, без пути из PATH."""
    return effective_python()


def child_env(base=None, **extra) -> dict:
    env = dict(base or os.environ)
    for k in [k for k in env if k.upper() in ("PYTHONHOME", "PYTHONPATH")]:
        del env[k]
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(extra)
    return env


def status(include_candidates: bool = False, force: bool = False) -> dict:
    """Сводка для панели и CLI. По умолчанию без обхода всех интерпретаторов: это дорого."""
    got = current(force=force)
    cfg = read_config()
    out = {
        "config": str(CONFIG),
        "configured": got.get("configured", False),
        "ok": got.get("ok", False),
        "reason": got.get("reason", ""),
        "runtime": got.get("runtime") or {},
        "effective_python": got.get("effective_python") or "",
        "base": got.get("base") or {},
        "pydantic": got.get("pydantic") or {},
        "platform": sys.platform,
        "os": os.name,
        "config_version": cfg.get("version", 0),
    }
    if include_candidates:
        out["candidates"] = candidates()
    return out


def bootstrap(path=None, check_pydantic: bool = False, install: bool = False,
              force: bool = False) -> dict:
    """Первый запуск одной цепочкой: найти → проверить → venv → проверить venv → поставить
    Pydantic AI → проверить импорт → только после успеха записать конфигурацию.

    Сбой любого шага не оставляет полурабочее состояние: конфигурация не пишется, причина
    возвращается вместе с кандидатами. Не запускается при импорте: создание venv и установка
    пакетов — видимые действия.
    """
    got = current(force=True)
    if got.get("configured") and got.get("ok") and not force:
        runtime = dict(got.get("runtime") or {})
        effective = got.get("effective_python") or ""
        info = pydantic_info(effective)
        if install and not info.get("ok"):
            # Конфигурация уже записана и работает, но Pydantic AI в ней нет: доставить в
            # тот же effective_python, а не в текущий процесс.
            installed = install_pydantic(python=effective)
            info = installed.get("pydantic") or {}
            if not installed.get("ok") or not info.get("ok"):
                return {"ok": False, "existing": True,
                        "error": "Pydantic AI не установлен или не импортируется: "
                                 + str(installed.get("error") or info.get("error")
                                       or "причина неизвестна"),
                        "runtime": runtime, "pydantic": info, "python": effective}
            runtime["pydantic_ai_version"] = info.get("version", "")
            cfg = read_config()
            cfg["version"] = CONFIG_VERSION
            cfg["runtime"] = runtime
            _write_config(cfg)
            _drop_cache()
        return {"ok": True, "runtime": runtime, "base": got.get("base") or {},
                "existing": True, "pydantic": info}
    found = validate_python(sys.executable)
    if not found.get("valid"):
        # Текущий процесс мог быть запущен через рабочую ссылку, хотя sys.executable
        # называет отсутствующий файл. Тогда берём лучший найденный, а не ломаемся.
        found = best()
    if not found.get("valid"):
        return {"ok": False, "error": "нет валидного Python", "candidates": candidates()}
    made = configure(found.get("source", ""), found["executable"], isolated=True,
                     path=path, check_pydantic=False, write=False)
    if not made.get("ok"):
        return made
    runtime = made.get("runtime") or {}
    effective = runtime.get("effective_python") or ""
    checked = validate_python(effective)
    if not checked.get("valid"):
        return {"ok": False, "error": f"venv создан, но не запускается: {checked.get('reason')}",
                "runtime": runtime, "base": found, "venv": runtime.get("venv_path") or "",
                "candidates": candidates()}
    if install:
        installed = install_pydantic(python=effective)
        info = installed.get("pydantic") or {}
        if not installed.get("ok") or not info.get("ok"):
            return {"ok": False,
                    "error": "Pydantic AI не установлен или не импортируется: "
                             + str(installed.get("error") or info.get("error") or "причина неизвестна"),
                    "runtime": runtime, "base": found, "pydantic": info,
                    "python": installed.get("python") or effective,
                    "candidates": candidates()}
        runtime["pydantic_ai_version"] = info.get("version", "")
    if check_pydantic and not runtime.get("pydantic_ai_version"):
        info = pydantic_info(effective)
        runtime["pydantic_ai_version"] = info.get("version", "") if info.get("ok") else ""
    cfg = read_config()
    cfg["version"] = CONFIG_VERSION
    cfg["runtime"] = runtime
    _write_config(cfg)
    _drop_cache()
    return {"ok": True, "runtime": runtime, "base": found, "pydantic": pydantic_info(effective)}


def pip_install(python: str, specs: list, timeout: float = 1800) -> dict:
    """Установка в конкретный интерпретатор с понятным PEP 668. Без `python3` из PATH."""
    checked = validate_python(python)
    if not checked.get("valid"):
        return {"ok": False, "error": f"Python не прошёл проверку: {checked['reason']}",
                "externally_managed": False}
    env = child_env()
    try:
        p = subprocess.run([checked["executable"], "-I", "-X", "utf8", "-m", "pip",
                            "install", *specs], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "externally_managed": checked["externally_managed"]}
    text = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0 and ("externally-managed" in text.lower()
                              or "externally managed" in text.lower()):
        return {"ok": False, "error": "PEP 668: этот Python управляется системой. "
                "Переключите runtime в venv и повторите.",
                "externally_managed": True, "output": text[-2000:]}
    return {"ok": p.returncode == 0, "error": "" if p.returncode == 0 else text[-2000:],
            "externally_managed": checked["externally_managed"], "output": text[-2000:]}


def install_pydantic(specs: list | None = None, timeout: float = 1800,
                     python: str = "") -> dict:
    """Поставить Pydantic AI в effective_python и проверить провайдеры.

    Версия не закрепляется: нужна любая, где есть `pydantic_ai.providers`. Пиннинг старой
    ветки ломал импорт (`No module named 'pydantic_ai.providers'`).

    `python` задаётся явно там, где конфигурация ещё не записана (`bootstrap`): иначе
    установка ушла бы в текущий процесс, а проверка — в venv, то есть в разные Python.
    """
    exe = python or python_or_self()
    res = pip_install(exe, list(specs or ["pydantic-ai"]), timeout=timeout)
    info = pydantic_info(exe)
    cfg = read_config()
    rt = cfg.get("runtime") or {}
    if rt.get("effective_python") and os.path.realpath(rt["effective_python"]) == os.path.realpath(exe):
        # Установка из карточки, а не через bootstrap: конфигурация уже записана, но
        # версия Pydantic AI в ней должна обновиться — иначе следующий статус соврёт.
        rt = dict(rt)
        rt["pydantic_ai_version"] = info.get("version", "") if info.get("ok") else ""
        cfg["version"] = CONFIG_VERSION
        cfg["runtime"] = rt
        _write_config(cfg)
        _drop_cache()
    return {**res, "python": exe, "pydantic": info}


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Aurora: единый Python и Pydantic AI")
    ap.add_argument("--status", action="store_true", help="конфигурация и найденные интерпретаторы")
    ap.add_argument("--detect", action="store_true", help="только найти интерпретаторы")
    ap.add_argument("--configure", action="store_true", help="выбрать и сохранить runtime")
    ap.add_argument("--bootstrap", action="store_true",
                    help="первый запуск: найти, проверить, создать venv и поставить Pydantic AI")
    ap.add_argument("--no-install", action="store_true",
                    help="не ставить зависимости при --bootstrap")
    ap.add_argument("--base", default="", help="путь к базовому Python")
    ap.add_argument("--source", default="", help="python.org|homebrew|system|store|custom")
    ap.add_argument("--venv", default="", help="путь venv")
    ap.add_argument("--global", dest="system_wide", action="store_true",
                    help="разрешить глобальный режим, если установка не PEP 668")
    ap.add_argument("--check-pydantic", action="store_true",
                    help="проверить pydantic_ai.providers в effective_python")
    ap.add_argument("--install-pydantic", action="store_true",
                    help="поставить Pydantic AI в effective_python")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.detect:
        data = candidates()
    elif a.bootstrap:
        data = bootstrap(path=a.venv or None, check_pydantic=a.check_pydantic,
                         install=not a.no_install)
    elif a.install_pydantic:
        data = install_pydantic()
    elif a.configure:
        data = configure(a.source, a.base, isolated=not a.system_wide,
                         path=a.venv or None, check_pydantic=a.check_pydantic)
    else:
        data = status(include_candidates=True)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        if "candidates" in data:
            print(f"runtime: {data.get('effective_python') or '—'} "
                  f"({'ok' if data.get('ok') else data.get('reason') or 'не выбран'})")
            for x in data["candidates"].get("valid", []):
                print(f"  ✓ {x['source']:10s} {x['version']:9s} {x['executable']}")
            for x in data["candidates"].get("invalid", []):
                print(f"  ✗ {x['path']}: {x['reason']}")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0 if (data.get("ok") or a.detect) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
