"""diagnostics.py — архив для разбора ошибки из панели.

Кнопка «Лог» у тревоги, ошибки или в шапке собирает один zip: версии, окружение,
состояние Pydantic AI и venv, журнал процесса адаптера, хвост прогона и выбранную
ошибку. Архив отдаётся браузеру и никуда не пишется: в проекте не появляется файла,
который пришлось бы прятать от git или делить между пользователями.

Ключи, токены, пароли и логины прокси вырезаются. Тела промптов и ответов модели в
архив не попадают: из `failures.jsonl` берутся только ход вызова и причина. Пути
домашней папки заменяются на `~`, путь проекта — на `<project>`.
"""
from __future__ import annotations

import hashlib
import io
import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
MAX_BUNDLE = 25 * 1024 * 1024
MAX_TEXT = 180_000
MAX_CONSOLE = 160_000
MAX_FAILURES = 80
SECRET_KEY = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PAT|CREDENTIALS?)(?:$|_)", re.I)


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _version(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _sha(path: str | Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _text(value, project: str = "", limit: int = MAX_TEXT) -> str:
    """Строка без домашнего пути, пути проекта и секретов."""
    s = str(value if value is not None else "")
    if project:
        s = s.replace(os.path.abspath(project), "<project>")
        s = s.replace(project, "<project>")
    s = s.replace(str(Path.home()), "~")
    s = re.sub(r"(?i)([A-Z]:\\Users\\)[^\\\s]+", r"\1<user>", s)
    s = re.sub(r"(?i)(/Users/|/home/)[^/\s]+", r"\1<user>", s)
    s = re.sub(r"(?i)\b(api[_-]?key|authorization|bearer|token|secret|password|pat)\b"
               r"(\s*[:=]\s*|\s+)[^\s,;]+", r"\1\2***", s)
    s = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "sk-***", s)
    s = re.sub(r"\b(?:ghp_|gho_|ghu_|ghs_|github_pat_|xox[baprs]-|AIza)"
               r"[A-Za-z0-9_-]{10,}", "<key>", s)
    s = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*", "<private-key>", s)
    s = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1***@", s)
    return s[:limit]


def _clean(value, project: str = ""):
    """Рекурсивно привести данные к JSON, вычистив строки."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value, project)
    if isinstance(value, dict):
        return {str(k): _clean(v, project) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(x, project) for x in value]
    return _text(value, project)


def _path_shape(path: str) -> dict:
    """Форма пути без имени проекта: для Windows важно, есть ли диск, пробелы и не-ASCII."""
    raw = str(path or "")
    p = Path(raw)
    return {"drive": p.drive, "parts": len(p.parts), "chars": len(raw),
            "non_ascii": any(ord(ch) > 127 for ch in raw), "space": " " in raw,
            "separator": "\\" if "\\" in raw else "/"}


def _read_tail(path: Path, limit: int = MAX_TEXT) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max(1, int(limit))))
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def _run(argv: list[str], cwd: str | None, env: dict | None, timeout: float) -> dict:
    try:
        p = subprocess.run(argv, cwd=cwd or None, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return {"ok": p.returncode == 0, "rc": p.returncode, "out": p.stdout[-20000:],
                "err": p.stderr[-20000:]}
    except Exception as e:  # noqa: BLE001 — архив не должен падать из-за проверки
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _system() -> dict:
    out = {
        "at": _stamp(),
        "system": platform.system(), "release": platform.release(),
        "version": platform.version(), "machine": platform.machine(),
        "processor": platform.processor(), "platform": platform.platform(),
        "os_name": os.name, "python_bits": 64 if sys.maxsize > 2 ** 32 else 32,
        "python": platform.python_version(), "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "prefix": sys.prefix, "base_prefix": getattr(sys, "base_prefix", sys.prefix),
        "cwd": os.getcwd(), "argv": list(sys.argv),
        "locale": list(locale.getlocale()), "preferred_encoding": locale.getpreferredencoding(False),
        "stdout_encoding": getattr(sys.stdout, "encoding", ""),
        "stderr_encoding": getattr(sys.stderr, "encoding", ""),
        "stdout_errors": getattr(sys.stdout, "errors", ""),
        "filesystem_encoding": sys.getfilesystemencoding(),
        "default_encoding": sys.getdefaultencoding(),
        "flags": {"utf8": bool(getattr(sys.flags, "utf8_mode", 0)),
                  "no_site": bool(getattr(sys.flags, "no_site", 0)),
                  "isolated": bool(getattr(sys.flags, "isolated", 0))},
        "cpu_count": os.cpu_count(),
        "which": {name: shutil.which(name) for name in ("python", "python3", "py")},
        "sys_path": list(sys.path),
    }
    if sys.platform == "win32":
        try:
            out["win32_ver"] = list(platform.win32_ver())
            out["windows_version"] = dict(sys.getwindowsversion()._asdict())
        except Exception:  # noqa: BLE001
            pass
        out["code_page"] = _run(["chcp"], None, None, 10).get("out", "").strip()
    return out


def _env_facts() -> dict:
    wanted = re.compile(
        r"^(?:PYTHON|VIRTUAL_ENV|CONDA|APPDATA|LOCALAPPDATA|TEMP|TMP|USERPROFILE|HOME|"
        r"PATH|PATHEXT|COMSPEC|SYSTEMROOT|WINDIR|PROCESSOR|NUMBER_OF_PROCESSORS|LANG|LC_|"
        r"TZ|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|SSL_|REQUESTS_CA|CURL_CA|AURORA_|PIP_|UV_|"
        r"HOMEDRIVE|HOMEPATH|USERNAME|USERDOMAIN)", re.I)
    out = {}
    for key, value in os.environ.items():
        if not wanted.search(key):
            continue
        val = str(value)
        if SECRET_KEY.search(key) or "TOKEN" in key.upper() or "PASSWORD" in key.upper():
            val = "***"
        else:
            val = _text(val, limit=8000)
        out[key] = val
    return out


def _venv_facts(AG) -> dict:
    """Что стоит в Python, которым запускается адаптер. Только чтение, без установки."""
    try:
        vpy = str(AG._venv_python())
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    if not os.path.isfile(vpy):
        return {"python": vpy, "exists": False}
    script = r"""
import json, sys
from importlib import metadata
names = ["pydantic-ai", "pydantic-ai-slim", "pydantic", "httpx", "openai", "mcp",
         "anyio", "typing-extensions", "uvicorn", "starlette", "graphify"]
packages = {}
for name in names:
    try:
        packages[name] = metadata.version(name)
    except Exception:
        packages[name] = ""
all_pkgs = {}
try:
    for d in metadata.distributions():
        n = (d.metadata.get("Name") or "").strip()
        v = (d.version or "").strip()
        if n and v:
            all_pkgs[n] = v
except Exception as e:
    all_pkgs = {"<ошибка>": type(e).__name__}
imports = {}
for name in ("pydantic_ai", "pydantic", "httpx", "openai", "mcp", "anyio"):
    try:
        __import__(name)
        imports[name] = {"ok": True, "error": ""}
    except Exception as e:
        imports[name] = {"ok": False, "error": type(e).__name__ + ": " + str(e)}
print(json.dumps({"python": sys.executable, "version": sys.version,
                  "prefix": sys.prefix, "base_prefix": getattr(sys, "base_prefix", sys.prefix),
                  "sys_path": sys.path, "packages": packages, "installed": all_pkgs,
                  "imports": imports}, ensure_ascii=False))
"""
    env = AG.venv_env() if hasattr(AG, "venv_env") else None
    r = _run([vpy, "-I", "-c", script], None, env, 40)
    out = {"python": vpy, "exists": True, "run": r}
    cfg = Path(vpy).parent.parent / "pyvenv.cfg"
    out["pyvenv_cfg"] = _text(_read_tail(cfg, 4000))
    try:
        out.update(json.loads(r.get("out") or "{}"))
    except ValueError:
        pass
    pip = _run([vpy, "-I", "-m", "pip", "check"], None, env, 40)
    out["pip_check"] = {"rc": pip.get("rc"), "out": (pip.get("out") or "")[-8000:],
                        "error": pip.get("error", "")}
    return out


def _adapter_facts(AG, project: str, deep: bool) -> dict:
    out = {
        "argv": _clean(_safe(AG, "_adapter_argv", []), project),
        "file": _text(_safe(AG, "_adapter_file", ""), project),
        "sha": _safe(AG, "_adapter_sha", ""),
        "gap": _text(_safe(AG, "adapter_gap", ""), project),
        "other_home": _text(_safe(AG, "other_home_hint", ""), project),
        "venv": _safe(AG, "venv_status", (False, "")),
        "venv_path": _text(_safe(AG, "venv_display_path", ""), project),
        "runtime": _clean(_safe(AG, "_runtime_status", {}), project),
        "selfcheck": _clean(_safe(AG, "last_selfcheck", {}), project),
        "state": _clean(getattr(AG, "ADAPTER", {}), project),
    }
    try:
        out["pydantic"] = _clean(AG.pydantic_settings(AG.config()), project)
    except Exception as e:  # noqa: BLE001
        out["pydantic"] = {"error": f"{type(e).__name__}: {e}"}
    hub = getattr(AG, "_HUB", None)
    if isinstance(hub, dict):
        proc = hub.get("proc")
        out["hub"] = {
            "pid": getattr(proc, "pid", None), "alive": bool(proc and proc.poll() is None),
            "deaths": hub.get("deaths"), "broken": _text(hub.get("broken"), project),
            "version": hub.get("version"), "pending": len(hub.get("pending") or {}),
        }
    if deep:
        try:
            out["probe"] = _clean(AG.adapter_probe(
                os.path.join(project, ".aurora", "scripts", "agents", "pydantic_ai_adapter.py")
                if project else None, project or None, wait=15), project)
        except Exception as e:  # noqa: BLE001
            out["probe"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _safe(obj, name: str, fallback):
    value = getattr(obj, name, fallback)
    if callable(value):
        try:
            return value()
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"
    return value


def _alarm_facts(AG, context: dict) -> dict:
    rows = _clean(_safe(AG, "read_alarm", {}), context.get("project", ""))
    key = str(context.get("key") or "")
    if isinstance(rows, dict):
        for row in rows.values():
            if isinstance(row, dict) and row.get("project"):
                row["project"] = "<project>"     # тревога общая на машину: чужой путь не показываем
    return {"selected": key, "row": rows.get(key, {}) if isinstance(rows, dict) else {},
            "all": rows}


def _latest_run(project: str, run_id: str = "") -> str:
    base = Path(project) / ".aurora" / "runs"
    if run_id and re.fullmatch(r"[\w-]{1,64}", run_id) and (base / run_id).is_dir():
        return run_id
    try:
        dirs = [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return ""
    return max(dirs, key=lambda p: p.stat().st_mtime).name if dirs else ""


def _slim_failure(obj: dict) -> dict:
    keep = {k: obj.get(k) for k in ("at", "task", "subject", "stage", "why") if k in obj}
    call = obj.get("call")
    if isinstance(call, dict):
        keep["call"] = {k: call.get(k) for k in (
            "role", "thinking", "max_tokens", "request_timeout", "deadline_s", "prompt_chars",
            "prompt_tokens_est", "history", "tools", "mcp_active", "adapter", "attempts",
            "rings", "ok", "backend", "model", "seconds", "waited", "ring", "finish",
            "tokens_in", "tokens_out", "tps", "via", "bypass", "log") if k in call}
    return keep


def _run_facts(project: str, run_id: str, context: dict) -> dict:
    rid = _latest_run(project, run_id) if project else ""
    out = {"run_id": rid, "project_path": _path_shape(project), "console": "", "failures": []}
    if not rid:
        return out
    base = Path(project) / ".aurora" / "runs" / rid
    out["console"] = _text(_read_tail(base / "console.log", MAX_CONSOLE), project)
    try:
        lines = (base / "failures.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines[-MAX_FAILURES:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out["failures"].append(_clean(_slim_failure(row), project))
    pulses = []
    for path in sorted(base.glob("pulse-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            pulses.append(_clean(data, project))
    out["pulses"] = pulses
    return out


def _project_facts(project: str) -> dict:
    if not project:
        return {}
    root = Path(project)
    core = root / ".aurora" / "scripts" / "agent_core.py"
    adapter = root / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py"
    return {
        "path": "<project>", "path_shape": _path_shape(project),
        "engine_version": _version(root / ".aurora" / "engine_version.txt"),
        "engine_file": {"exists": core.is_file(), "sha": _sha(core)},
        "adapter_file": {"exists": adapter.is_file(), "sha": _sha(adapter)},
        "kit_version": _version(KIT / "VERSION"),
    }


def _ui_version() -> str:
    try:
        m = re.search(r'const UI_VERSION\s*=\s*"([^"]+)"',
                      (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8"))
        return m.group(1) if m else ""
    except OSError:
        return ""


def collect(project: str = "", context: dict | None = None, run_id: str = "",
            deep: bool = False) -> dict:
    """Собрать всё, что нужно для разбора: без сети и без записи в проект."""
    import agent_core as AG
    context = dict(context or {})
    project = str(project or context.get("project") or "")
    key = str(context.get("key") or "")
    raw_alarm = _safe(AG, "read_alarm", {})
    row = raw_alarm.get(key) if isinstance(raw_alarm, dict) else None
    if isinstance(row, dict) and os.path.isdir(str(row.get("project") or "")):
        # Тревога общая на машину: её проект важнее раздела, из которого нажали «Лог».
        project = str(row["project"])
    if project and not os.path.isdir(project):
        project = ""
    context.setdefault("aurora_home",
                       _text(str(getattr(getattr(AG, "RT", None), "HOME", ""))))
    context["project"] = "<project>" if project else ""
    alarm = _alarm_facts(AG, {**context, "project": project})
    data = {
        "schema": 1,
        "generated": _stamp(),
        "context": _clean(context, project),
        "system": _clean(_system(), project),
        "env": _clean(_env_facts(), project),
        "project": _project_facts(project),
        "ui_version": _ui_version(),
        "alarm": alarm,
        "adapter": _adapter_facts(AG, project, deep),
        "run": _run_facts(project, run_id, context),
        "adapter_log": _text(_safe(AG, "adapter_log_tail", ""), project, 120_000),
    }
    if not data["adapter_log"]:
        try:
            data["adapter_log"] = _text(_read_tail(Path(getattr(AG.RT, "HOME")) / "logs" / "adapter.log",
                                                   120_000), project)
        except Exception:  # noqa: BLE001
            pass
    try:
        data["venv"] = _clean(_venv_facts(AG), project)
    except Exception as e:  # noqa: BLE001
        data["venv"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        import access_check as AC
        me = AC.whoami()
        others = AC.others("pydantic-ai", me)
        for row in others:
            if isinstance(row, dict):
                row["user"] = "<user>"       # чужие имена в архив не переносим
        data["who"] = _clean({"me": me, "python_env": AC.python_env_set(),
                              "others": others}, project)
    except Exception as e:  # noqa: BLE001
        data["who"] = {"error": f"{type(e).__name__}: {e}"}
    return _clean(data, project)


def _summary(data: dict) -> str:
    a = data.get("adapter") or {}
    v = data.get("venv") or {}
    s = data.get("system") or {}
    p = data.get("project") or {}
    c = data.get("context") or {}
    state = a.get("state") if isinstance(a.get("state"), dict) else {}
    lines = [
        "# Aurora diagnostics", "",
        f"Собрано: {data.get('generated')}",
        f"Источник: {c.get('source') or 'панель'}",
        f"Проект: {p.get('path') or '—'} · движок {p.get('engine_version') or '—'} · "
        f"кит {p.get('kit_version') or '—'} · панель {data.get('ui_version') or '—'}",
        f"ОС: {s.get('system')} {s.get('release')} ({s.get('machine')}) · "
        f"Python {s.get('python')} · кодировка {s.get('preferred_encoding')}",
        f"AURORA_HOME: {data.get('context', {}).get('aurora_home', '—')}",
        "", "## Выбранная ошибка", "",
        f"Ключ: {c.get('key') or '—'} · область: {c.get('area') or '—'} · "
        f"вид: {c.get('kind') or '—'}",
        f"Текст: {c.get('message') or c.get('why') or '—'}",
        "", "## Pydantic AI", "",
        f"Пакет: {v.get('packages', {}).get('pydantic-ai') or '—'} · "
        f"Python venv: {v.get('version') or '—'}",
        f"Адаптер: {a.get('file') or '—'} · sha {a.get('sha') or '—'}",
        f"Состояние: {state}",
        f"Проверка запуска: {a.get('probe') or 'в этот раз не запускалась'}",
        f"Причина обхода: {a.get('gap') or state.get('fallback_why') or '—'}",
        "", "## Запуск", "",
        f"run_id: {(data.get('run') or {}).get('run_id') or '—'}",
        f"Журнал адаптера: {len(data.get('adapter_log') or '')} символов",
        f"Сбоев в хвосте: {len((data.get('run') or {}).get('failures') or [])}",
        "", "## Что внутри", "",
        "`diagnostics.json` — полные данные, `adapter.log` — stderr/stdout адаптера, "
        "`run/console.log` — хвост консоли, `run/failures.jsonl` — сбои без тел ответов, "
        "`venv.json` — пакеты и импорт, `env.json` — только нужные переменные с маской.",
    ]
    return "\n".join(lines) + "\n"


def _readme() -> str:
    return (
        "# Архив диагностики Aurora\n\n"
        "Собран кнопкой «Лог» в панели. Он нужен, чтобы разобрать ошибку, и не является "
        "настройкой проекта.\n\n"
        "В архиве нет API-ключей, токенов и паролей: они заменены на `***`. Полные тела "
        "промптов и ответов модели из `failures.jsonl` не включены, оставлен только ход "
        "вызова; `console.log` — вывод команды и может содержать текст проекта. "
        "Домашний путь заменён на `~`, путь проекта — на `<project>`.\n\n"
        "Приложите zip к сообщению об ошибке. Если в логах остались данные, которые нельзя "
        "передавать, удалите соответствующие строки перед отправкой.\n"
    )


def build(project: str = "", context: dict | None = None, run_id: str = "",
          deep: bool = False) -> tuple[str, bytes]:
    """Собрать zip в памяти: (имя файла, байты). В проект ничего не пишется."""
    import agent_core as AG
    context = dict(context or {})
    if project:
        context.setdefault("aurora_home",
                           _text(str(getattr(getattr(AG, "RT", None), "HOME", ""))))
    data = collect(project, context, run_id=run_id, deep=deep)
    files: dict[str, str] = {
        "README.md": _readme(),
        "summary.md": _summary(data),
        "diagnostics.json": json.dumps(data, ensure_ascii=False, indent=1),
        "adapter.log": data.pop("adapter_log", ""),
        "env.json": json.dumps(data.get("env") or {}, ensure_ascii=False, indent=1),
        "venv.json": json.dumps(data.get("venv") or {}, ensure_ascii=False, indent=1),
    }
    run = data.get("run") or {}
    files["run/console.log"] = run.get("console") or ""
    files["run/failures.jsonl"] = "\n".join(
        json.dumps(x, ensure_ascii=False) for x in (run.get("failures") or [])) + "\n"
    files["run/pulses.json"] = json.dumps(run.get("pulses") or [], ensure_ascii=False, indent=1)
    # `diagnostics.json` уже содержит хвост консоли и сбои; отдельные файлы помогают
    # открыть их без разбора JSON, но не удваивают архив без меры.
    data.pop("run", None)
    files["diagnostics.json"] = json.dumps(data, ensure_ascii=False, indent=1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            body = (text or "").encode("utf-8", "replace")
            if sum(i.file_size for i in z.infolist()) + len(body) > MAX_BUNDLE:
                body = body[:max(0, MAX_BUNDLE // 2)]
            z.writestr(name, body)
    name = "aurora-diagnostics-" + time.strftime("%Y%m%d-%H%M%S") + ".zip"
    return name, buf.getvalue()
