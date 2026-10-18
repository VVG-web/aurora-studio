#!/usr/bin/env python3
"""conn_gate.py — что нужно делу и доступно ли оно: источники проекта, провайдеры моделей, команды.

`connectivity.py` отвечает на вопрос «что сейчас доступно» (профили, интернет, провайдеры). Здесь — вторая
половина: **что нужно** конкретной работе и **можно ли** её начинать.

- **режим доступа источника** — блок `source_access:` в `aurora.config.yaml` проекта (ключ — id источника, имя папки
  зеркала). Режимы: `internet` — «не требует VPN, требует интернет» (веб), `vpn` — «требует профиль X», `local` —
  «локальный или из кэша, сети не требует», `mixed` — «VPN плюс локальный кэш». Признаки: `internet` (нужен интернет
  независимо от VPN) и `offline_cache` (доступен из кэша без связи). В проекте только ИМЯ профиля — адресов нет, они
  лежат на машине. Нет записи — берётся умолчание модуля источника (`access` в его манифесте: у веба — `internet`),
  а нет и его — источник считается доступным, как было всегда;
- **метаданные команд** — файл `command_deps.json` (рядом с реестром команд): какие источники нужны, нужна ли модель,
  может ли команда работать без связи. Типы 1, 2, 3 — только ярлыки для показа, логика на них не стоит;
- **требования** команды в проекте (`command_requirements`) — профили и интернет, вычисленные из источников и провайдеров;
- **вердикт** (`verdict`) — доступно ли нужное сейчас, и если нет — что именно и почему; решение «что с этим делать»
  (отказать, пропустить, отложить, ждать) принимает политика (`policy_for`, `plan_steps`).

Функция выключена, пока владелец не включил проверку соединений (`connectivity.py`): без неё вердикт всегда «можно».
Модуль только читает; настройки пишет панель.

  python3 scripts/conn_gate.py --derive            # записи command_deps.json для команд, которых там ещё нет
  python3 scripts/conn_gate.py --check             # command_deps.json против реестра команд (для разработчика кита)
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import json
import os
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

import connectivity as CN  # noqa: E402

ACCESS_KEY = "source_access"
MODES = ("internet", "vpn", "local", "mixed")
ACCESS_FIELDS = ("mode", "vpn", "internet", "offline_cache")
CONFIG_FILE = "aurora.config.yaml"
BOM = "﻿"
DEPS_NAME = "command_deps.json"
DEPS_FIELDS = ("requires_sources", "requires_llm", "requires_internet", "offline_capable", "offline_variant")
_TRUE = ("true", "yes", "да", "1", "on")
_FALSE = ("false", "no", "нет", "0", "off")
_NAME_OK = re.compile(r"^[A-Za-z0-9_.-]+$")


# ------------------------------------------------------------------ блок source_access: в конфиге проекта

def _span(lines: list):
    """(первая строка, строка после последней) блока `source_access:` верхнего уровня; None — блока нет.

    Блок — строка ключа и всё вложенное под ней; пустые строки и комментарии без отступа в конце блока ему
    не принадлежат. Закомментированный пример блоком не считается."""
    start = None
    for i, line in enumerate(lines):
        if line[:1] in ("", " ", "\t", "#", "\r"):
            continue
        if line.split("#", 1)[0].rstrip().startswith(ACCESS_KEY + ":"):
            start = i
            break
    if start is None:
        return None
    last = start
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line[0] == "#":
            continue
        if line[0] in (" ", "\t"):
            last = j
        else:
            break
    return start, last + 1


def _unquote(raw: str) -> str:
    v = raw.strip()
    if not v:
        return ""
    if v[0] in "\"'":
        quote = v[0]
        out, i = [], 1
        while i < len(v):
            ch = v[i]
            if quote == '"' and ch == "\\" and v[i + 1:i + 2] in ('"', "\\"):
                out.append(v[i + 1])
                i += 2
                continue
            if quote == "'" and ch == "'" and v[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            if ch == quote:
                break
            out.append(ch)
            i += 1
        return "".join(out)
    if v[0] == "#":
        return ""
    cut = v.find(" #")
    return (v[:cut] if cut >= 0 else v).strip()


def _quote(value: str) -> str:
    if '"' in value or "\\" in value:
        return "'" + value.replace("'", "''") + "'"
    return '"' + value + '"'


def _split_key(line: str):
    """«  "Имя (старое)": значение» → (ключ, остаток) с учётом кавычек у ключа."""
    s = line.strip()
    if s[:1] in "\"'":
        q = s[0]
        end = s.find(q, 1)
        while end != -1 and q == '"' and s[end - 1] == "\\":
            end = s.find(q, end + 1)
        if end == -1 or s[end + 1:end + 2] != ":":
            return None, ""
        return _unquote(s[:end + 1]), s[end + 2:]
    key, sep, rest = s.partition(":")
    return (key.strip(), rest) if sep else (None, "")


def parse_access(config_text: str) -> tuple:
    """(режимы доступа, проблемы) из блока `source_access:` → {id источника: {mode, vpn, internet, offline_cache}}.

    Читает построчно, без YAML-библиотеки; негодное пропускает и называет в проблемах, а не падает на ручной правке."""
    lines = str(config_text).removeprefix(BOM).replace("\r\n", "\n").split("\n")
    span = _span(lines)
    if span is None:
        return {}, []
    found: dict = {}
    problems: list = []
    base = sid = None
    for raw in lines[span[0] + 1:span[1]]:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, rest = _split_key(raw)
        if key is None:
            problems.append(f"{ACCESS_KEY}: строка «{s[:60]}» — не «ключ: значение»; пропущена")
            continue
        value = _unquote(rest)
        if base is None:
            base = indent
        if indent <= base:                                   # строка источника
            sid = None
            if not key or len(key) > 120 or any(ord(c) < 32 for c in key):
                problems.append(f"{ACCESS_KEY}: имя источника «{key[:40]}» не годится; блок пропущен")
            elif value:
                problems.append(f"{ACCESS_KEY}: у «{key}» значение в одной строке — поля пишутся вложенными строками")
            else:
                sid = key
                found.setdefault(sid, {})
            continue
        if sid is None:
            continue
        rec = found[sid]
        word = value.strip().lower()
        if key == "mode":
            if word in MODES:
                rec["mode"] = word
            elif word:
                problems.append(f"{ACCESS_KEY}: у «{sid}» режим «{value[:20]}» неизвестен ({', '.join(MODES)}); пропущен")
        elif key == "vpn":
            if not value or CN.NAME_RE.match(value):
                rec["vpn"] = value
            else:
                problems.append(f"{ACCESS_KEY}: у «{sid}» имя профиля «{value[:40]}» не годится; пропущено")
        elif key in ("internet", "offline_cache"):
            if word in _TRUE:
                rec[key] = True
            elif word in _FALSE:
                rec[key] = False
            elif word:
                problems.append(f"{ACCESS_KEY}: у «{sid}» «{key}» — true или false, а не «{value[:20]}»; пропущено")
        else:
            problems.append(f"{ACCESS_KEY}: у «{sid}» поле «{key[:40]}» не из {', '.join(ACCESS_FIELDS)}; пропущено")
    return found, problems


def clean_access(access) -> tuple:
    """Что прислала страница → (режимы к записи, [ошибки с кодами]). Негодное — ошибка, а не молчаливая правка."""
    errors: list = []
    out: dict = {}
    if not isinstance(access, dict):
        return {}, [{"code": "bad_access", "where": "", "text": "режимы доступа — объект {источник: поля}"}]
    for sid, rec in access.items():
        where = f"{ACCESS_KEY}.{sid}"
        if not isinstance(sid, str) or not sid.strip() or len(sid) > 120 or any(ord(c) < 32 for c in sid):
            errors.append({"code": "bad_source", "where": where, "text": "имя источника не годится"})
            continue
        if rec is None or rec == {}:
            continue
        if not isinstance(rec, dict):
            errors.append({"code": "bad_access", "where": where, "text": "поля источника — объект"})
            continue
        row = {}
        mode = rec.get("mode")
        if mode not in (None, ""):
            if mode not in MODES:
                errors.append({"code": "bad_mode", "where": where + ".mode", "text": "режим — internet, vpn, local или mixed"})
                continue
            row["mode"] = mode
        vpn = rec.get("vpn")
        if vpn not in (None, ""):
            if not isinstance(vpn, str) or not CN.NAME_RE.match(vpn.strip()):
                errors.append({"code": "bad_name", "where": where + ".vpn", "text": "имя профиля не годится"})
                continue
            row["vpn"] = vpn.strip()
        for key in ("internet", "offline_cache"):
            if rec.get(key) is None or rec.get(key) == "":
                continue
            if not isinstance(rec[key], bool):
                errors.append({"code": "bad_flag", "where": where + "." + key, "text": f"«{key}» — да или нет"})
                continue
            row[key] = rec[key]
        unknown = [k for k in rec if k not in ACCESS_FIELDS]
        if unknown:
            errors.append({"code": "bad_field", "where": where, "text": f"лишнее поле «{str(unknown[0])[:40]}»"})
            continue
        if row.get("mode") in ("vpn", "mixed") and not row.get("vpn"):
            row.pop("vpn", None)         # профиль не выбран — «как раньше»: доступен
        if row:
            out[sid] = row
    return out, errors


def _block_lines(access: dict) -> list:
    if not access:
        return []
    out = [ACCESS_KEY + ":"]
    for sid in sorted(access):
        key = sid if _NAME_OK.match(sid) else _quote(sid)
        out.append(f"  {key}:")
        row = access[sid]
        if "mode" in row:
            out.append(f'    mode: "{row["mode"]}"')
        if row.get("vpn"):
            out.append(f"    vpn: {_quote(row['vpn'])}")
        for k in ("internet", "offline_cache"):
            if k in row:
                out.append(f'    {k}: "{"true" if row[k] else "false"}"')
    return out


def write_access(config_text: str, access: dict) -> str:
    """Текст конфига с переписанным блоком `source_access:`; всё остальное — как было, байт в байт (окончания строк
    и метка порядка байтов сохраняются). Нет блока — добавляется в конец; пустые режимы убирают блок."""
    bom = BOM if config_text.startswith(BOM) else ""
    lines = config_text[len(bom):].split("\n")
    span = _span(lines)
    block = _block_lines(access)
    if span is None:
        if not block:
            return config_text
        last = next((ln for ln in reversed(lines) if ln.strip()), "")
        cr = "\r" if last.endswith("\r") else ""
        body = "\n".join(lines).rstrip("\r\n")
        return bom + (body + (cr + "\n") * 2 if body else "") + "\n".join(ln + cr for ln in block) + "\n"
    a, b = span
    cr = "\r" if lines[a].endswith("\r") else ""
    new = lines[:a] + [ln + cr for ln in block] + lines[b:]
    if not block:
        while 0 < a < len(new) and not new[a - 1].strip() and not new[a].strip():
            if a == len(new) - 1:
                del new[a - 1]
                a -= 1
            else:
                del new[a]
    return bom + "\n".join(new)


def access_digest(project: str) -> str:
    """Слепок блока `source_access:` как его видит движок — страница шлёт его при записи (`expect`)."""
    found, _problems = parse_access(_config_text(project))
    return CN.digest(found)


def _config_text(project: str) -> str:
    try:
        with open(os.path.join(project, CONFIG_FILE), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def save_access(project: str, access: dict, expect=None) -> dict:
    """Записать блок `source_access:` проекта → {ok, digest} либо {ok: False, code, error[, conflict]}.

    Чужие разделы конфига, комментарии и окончания строк остаются как были. `expect` — слепок блока, с которым страница
    его открыла: блок за это время менялся — отказ `conflict`. Запись неделимая; конфиг не в UTF-8 не трогается."""
    clean, errors = clean_access(access)
    if errors:
        return {"ok": False, "code": errors[0]["code"], "error": errors[0]["text"], "errors": errors}
    path = os.path.join(project, CONFIG_FILE)
    try:
        with open(path, encoding="utf-8", newline="") as f:
            old = f.read()
    except FileNotFoundError:
        return {"ok": False, "code": "no_config", "error": "в проекте нет aurora.config.yaml", "errors": []}
    except UnicodeDecodeError:
        return {"ok": False, "code": "not_utf8", "error": "aurora.config.yaml не в кодировке UTF-8 — файл не тронут", "errors": []}
    if expect is not None and expect != CN.digest(parse_access(old)[0]):
        return {"ok": False, "conflict": True, "code": "conflict", "errors": [],
                "error": "режимы доступа изменились на диске с момента открытия страницы"}
    new = write_access(old, clean)
    if new != old:
        try:
            _write_exact(path, new)
        except OSError as e:
            return {"ok": False, "code": "write", "failed": True, "errors": [],
                    "error": f"не удалось записать aurora.config.yaml: {e.strerror or e}"}
    return {"ok": True, "digest": CN.digest(parse_access(new)[0])}


def _write_exact(path: str, text: str) -> None:
    """Записать целиком или не трогать файл, без перевода строк (`\\r\\n` остаётся `\\r\\n`); права прежнего файла сохраняются."""
    import shutil
    from aurora_common import replace_file
    real = Path(os.path.realpath(path))
    tmp = real.with_name(f"{real.name}.{os.getpid()}.aurora-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        try:
            shutil.copymode(real, tmp)
        except OSError:
            pass
        replace_file(tmp, real)
    finally:
        tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------ источники проекта

def source_instances(project: str) -> list:
    """Включённые источники проекта с режимом доступа → [{id, module, title, role, mode, vpn, internet, offline_cache,
    origin}]. Режим — из `source_access:`; нет записи — умолчание модуля (`access.mode` манифеста); нет и его — `mode: ""`
    (источник доступен, как было всегда)."""
    try:
        import sources_registry as SR
        items = SR.instances(project)
    except Exception:  # noqa: BLE001 — нет реестра: источников нет, а не падение
        return []
    explicit, _problems = parse_access(_config_text(project))
    out = []
    for it in items:
        sid = it.get("id") or ""
        manifest = it.get("manifest") or {}
        base = manifest.get("access") if isinstance(manifest.get("access"), dict) else {}
        row = explicit.get(sid) or {}
        mode = row.get("mode") or (base.get("mode") if base.get("mode") in MODES else "")
        origin = "config" if row.get("mode") else "manifest" if mode else "default"
        out.append({
            "id": sid, "module": it.get("module", ""), "title": it.get("title", ""), "role": it.get("role", ""),
            "mode": mode, "default_mode": base.get("mode") if base.get("mode") in MODES else "",
            "vpn": row.get("vpn", "") if mode in ("vpn", "mixed") else "",
            "internet": row["internet"] if "internet" in row else mode == "internet",
            "offline_cache": bool(row.get("offline_cache", mode in ("local", "mixed"))),
            "origin": origin})
    return out


def needs_of_source(src: dict) -> dict:
    """Что нужно источнику, чтобы его читали по сети: {profile, internet}. Пустое — сеть ему не нужна."""
    return {"profile": src["vpn"] if src["mode"] in ("vpn", "mixed") else "",
            "internet": bool(src["internet"]) and src["mode"] != "local"}


def access_state(project: str) -> dict:
    """Всё для группы «Режим доступа источников» в «Настройках проекта»: источники с режимами, слепок блока, проблемы."""
    found, problems = parse_access(_config_text(project))
    return {"sources": source_instances(project), "modes": list(MODES), "explicit": found,
            "digest": CN.digest(found), "problems": problems}


# ------------------------------------------------------------------ что нужно делу

def models_used(kit=None) -> list:
    """Провайдеры моделей, стоящие хоть в одной включённой роли: то, на чём реально работает кит."""
    try:
        import model_config as MC
        k = CN.kit_dir(kit)
        data = MC.load(k) if k else {}
    except Exception:  # noqa: BLE001
        return []
    used = set()
    for cap in (data.get("capabilities") or {}).values():
        for role in cap.get("roles") or []:
            for b in role.get("backends") or []:
                if b.get("enabled", True) is not False:
                    used.add(b.get("provider"))
    return [p for p in CN.providers_of(kit, data) if p["id"] in used]


def required_for(project: str, svc=None) -> dict:
    """Что нужно делу проекта: {profiles, providers, internet} — по включённым источникам и провайдерам моделей.

    Глобальное состояние связи считается только по этим зависимостям, а не по всем описанным профилям."""
    profiles, internet = set(), False
    for src in source_instances(project):
        need = needs_of_source(src)
        if need["profile"]:
            profiles.add(need["profile"])
        internet = internet or need["internet"]
    provs = models_used(getattr(svc, "kit", None))
    for p in provs:
        if p["profile"]:
            profiles.add(p["profile"])
        elif not p["local"]:
            internet = True
    return {"profiles": sorted(profiles), "providers": [p["id"] for p in provs], "internet": internet}


# ------------------------------------------------------------------ метаданные команд

def deps_file() -> Path | None:
    """`command_deps.json` рядом с реестром команд: в ките — корень кита, в проекте — `.aurora/`."""
    here = Path(__file__).resolve().parent
    for p in (here.parent / DEPS_NAME, here.parent.parent / DEPS_NAME):
        if p.is_file():
            return p
    return None


_DEPS: dict = {"stamp": None, "rows": {}}


def load_deps(path=None) -> dict:
    """{команда: метаданные} с умолчаниями недостающих полей. Нет файла или он битый — пусто (ничего не блокируется)."""
    p = Path(path) if path else deps_file()
    if p is None:
        return {}
    try:
        stamp = (str(p), p.stat().st_mtime_ns)
        if _DEPS["stamp"] == stamp and path is None:
            return _DEPS["rows"]
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = {}
    for cmd, row in (data.get("commands") or {}).items() if isinstance(data, dict) else []:
        if isinstance(row, dict):
            rows[str(cmd)] = norm_deps(row)
    if path is None:
        _DEPS["stamp"], _DEPS["rows"] = stamp, rows
    return rows


def norm_deps(row: dict) -> dict:
    srcs = row.get("requires_sources")
    return {"requires_sources": [str(x) for x in srcs if isinstance(x, str)] if isinstance(srcs, list) else [],
            "requires_llm": row.get("requires_llm") is True,
            "requires_internet": row.get("requires_internet") is True,
            "offline_capable": row.get("offline_capable") is True,
            "offline_variant": str(row.get("offline_variant") or "")}


def deps_of(cmd: str, deps: dict | None = None) -> dict:
    """Метаданные команды; нет записи — «зависимостей нет»: команда запускается всегда."""
    rows = load_deps() if deps is None else deps
    return rows.get(cmd) or norm_deps({"offline_capable": True})


def net_types(d: dict) -> list:
    """Ярлыки 1/2/3 для показа: 1 — зависит от источников или сети, 2 — от модели, 3 — ни от того, ни от другого.

    Это подпись для человека, а не логика: решение о запуске принимают метаданные, а не ярлык."""
    out = []
    if d["requires_sources"] or d["requires_internet"]:
        out.append(1)
    if d["requires_llm"]:
        out.append(2)
    return out or [3]


def command_requirements(cmd: str, project: str, deps: dict | None = None, sources: list | None = None,
                         providers: list | None = None) -> dict:
    """Что нужно команде в проекте: {profiles, internet, llm, llm_profiles, sources[], offline_capable, types}.

    `profiles` — профили, без которых нужные источники не прочитать; `llm_profiles` — профили провайдеров моделей
    (хватит любого из провайдеров, поэтому это «хотя бы один из», а не «все»)."""
    d = deps_of(cmd, deps)
    srcs = source_instances(project) if sources is None and project else (sources or [])
    needed = []
    for mod in d["requires_sources"]:
        for s in srcs:
            if s["module"] == mod:
                needed.append(s)
    profiles, internet, rows = set(), d["requires_internet"], []
    for s in needed:
        n = needs_of_source(s)
        if n["profile"]:
            profiles.add(n["profile"])
        internet = internet or n["internet"]
        rows.append({"id": s["id"], "module": s["module"], "mode": s["mode"], "profile": n["profile"],
                     "internet": n["internet"], "offline_cache": s["offline_cache"]})
    llm_profiles, llm_internet = [], False
    if d["requires_llm"]:
        provs = models_used() if providers is None else providers
        llm_profiles = sorted({p["profile"] for p in provs if p["profile"]})
        llm_internet = any(not p["profile"] and not p["local"] for p in provs)
    return {"cmd": cmd, "profiles": sorted(profiles), "internet": bool(internet), "llm": d["requires_llm"],
            "llm_profiles": llm_profiles, "llm_internet": llm_internet, "sources": rows,
            "offline_capable": d["offline_capable"], "offline_variant": d["offline_variant"], "types": net_types(d)}


# ------------------------------------------------------------------ разработка кита: реестр и метаданные

def derive_entry(row: dict, by_command: dict) -> dict:
    """Умолчание метаданных команды по реестру: `sync:*` — источник по команде модуля; модель — нужна LLM; команда без
    источников и модели может работать без связи. Единичные команды правят руками в `command_deps.json`."""
    srcs = [by_command[row["cmd"]]] if row["cmd"] in by_command else []
    llm = row["kind"] in ("модель", "скрипт+модель")
    return {"requires_sources": srcs, "requires_llm": llm, "requires_internet": False,
            "offline_capable": not srcs and not llm, "offline_variant": ""}


def connector_commands(kit_root: str) -> dict:
    """{команда запуска: id модуля} по манифестам коннекторов кита."""
    out = {}
    base = Path(kit_root) / "connectors"
    for man in sorted(base.glob("*/connector.json")) if base.is_dir() else []:
        try:
            m = json.loads(man.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cmd = ((m.get("run") or {}).get("command") or "")
        if cmd and m.get("id"):
            out[cmd] = m["id"]
    return out


def check_deps(rows: list, kit_root: str, path=None) -> list:
    """Проблемы `command_deps.json` против реестра команд → список строк. Пусто — всё сходится."""
    p = Path(path) if path else Path(kit_root) / DEPS_NAME
    problems = []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{DEPS_NAME}: не читается ({e})"]
    have = data.get("commands") if isinstance(data, dict) and isinstance(data.get("commands"), dict) else {}
    names = {r["cmd"] for r in rows}
    for cmd in sorted(names - set(have)):
        problems.append(f"{DEPS_NAME}: нет записи для команды {cmd} (python3 scripts/conn_gate.py --derive)")
    for cmd in sorted(set(have) - names - {c for c in have if c.startswith("dev:")}):
        problems.append(f"{DEPS_NAME}: запись для несуществующей команды {cmd}")
    modules = set(connector_commands(kit_root).values())
    for cmd, row in have.items():
        if not isinstance(row, dict):
            problems.append(f"{DEPS_NAME}: {cmd}: запись — не объект")
            continue
        bad = [k for k in row if k not in DEPS_FIELDS]
        if bad:
            problems.append(f"{DEPS_NAME}: {cmd}: лишние поля {', '.join(bad)}")
        for key in ("requires_llm", "requires_internet", "offline_capable"):
            if key in row and not isinstance(row[key], bool):
                problems.append(f"{DEPS_NAME}: {cmd}: «{key}» — не да/нет")
        srcs = row.get("requires_sources", [])
        if not isinstance(srcs, list) or any(not isinstance(x, str) for x in srcs):
            problems.append(f"{DEPS_NAME}: {cmd}: «requires_sources» — не список имён модулей")
        elif modules:
            for m in srcs:
                if m not in modules:
                    problems.append(f"{DEPS_NAME}: {cmd}: модуля источника «{m}» нет среди коннекторов кита")
    return problems


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Метаданные зависимостей команд (для разработчика кита)")
    ap.add_argument("--derive", action="store_true", help="записи command_deps.json для команд, которых там ещё нет")
    ap.add_argument("--check", action="store_true", help="command_deps.json против реестра команд")
    a = ap.parse_args(argv)
    kit = str(Path(__file__).resolve().parent.parent)
    import kit_commands as K
    rows = K.read_registry()
    if a.check:
        problems = check_deps(rows, kit)
        for line in problems:
            print("ERROR:", line, file=sys.stderr)
        print(f"conn_gate --check: команд {len(rows)}, замечаний {len(problems)}")
        return 1 if problems else 0
    if a.derive:
        have = load_deps(Path(kit) / DEPS_NAME)
        by_cmd = connector_commands(kit)
        miss = {r["cmd"]: derive_entry(r, by_cmd) for r in rows if r["cmd"] not in have}
        print(json.dumps(miss, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
