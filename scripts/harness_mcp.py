#!/usr/bin/env python3
"""harness_mcp.py — MCP Авроры в ассистентах машины: найти, подключить, вернуть («Аврора»).

Сервер Авроры (`aurora_mcp.py --all`: база знаний всех проектов, ревью, Jira, память
бота) полезен в любом ассистенте — Claude Code, Cursor, Codex, OpenCode, Continue, Zed и
других. Подключать его руками в каждом — это два десятка разных файлов в разных форматах.
Здесь это делает Аврора: находит ассистентов на машине по каталогу `harnesses.json`
(он едет с китом и обновляется с ним), показывает, где Аврора уже подключена, и добавляет
её по кнопке.

Перед любой правкой файл ассистента копируется в `~/.aurora/harness-backups/<ассистент>/`;
вернуть копию можно из того же меню. Правка вставляется в текст, а не пересобирает файл:
комментарии и порядок полей человека остаются. После записи файл разбирается заново —
не разобрался, значит, копия возвращается сама.

Правка делается кодом, а не моделью: в этих файлах лежат ключи других MCP-серверов, и
отправлять их в LLM нельзя; к тому же код делает одно и то же каждый раз и проверяем.

    python3 scripts/harness_mcp.py --list           # какие ассистенты есть и где Аврора
    python3 scripts/harness_mcp.py --add cursor     # подключить Аврору к ассистенту
    python3 scripts/harness_mcp.py --restore cursor # вернуть файл из последней копии

Панель: «Установка» → «Aurora MCP в ассистентах».
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# `aurora_common` при импорте переводит вывод в UTF-8 (Windows: cp1251 в трубе падал на
# русских буквах) и даёт время по общему правилу движка.
from aurora_common import utc_slug  # noqa: E402

CATALOG = HERE / "harnesses.json"
NAME = "aurora"
VERSION_TIMEOUT = 15        # node-ассистенты (Gemini, Qwen) отвечают на --version секундами


class HarnessError(Exception):
    """Отказ: уходит человеку текстом причины."""


# ------------------------------------------------------------------ места и каталог

def platform_key() -> str:
    return {"darwin": "darwin", "win32": "windows"}.get(sys.platform, "linux")


def home() -> str:
    return os.path.expanduser("~")


def backups_root() -> Path:
    return Path(home()) / ".aurora" / "harness-backups"


def appdata() -> str:
    plat = platform_key()
    if plat == "darwin":
        return os.path.join(home(), "Library", "Application Support")
    if plat == "windows":
        return os.environ.get("APPDATA") or os.path.join(home(), "AppData", "Roaming")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(home(), ".config")


def expand(tpl: str) -> str:
    path = (tpl.replace("{vscode}", "{appdata}/Code/User")
            .replace("{appdata}", appdata()).replace("{home}", home()))
    return os.path.normpath(os.path.expanduser(path))


def catalog() -> list:
    with open(CATALOG, encoding="utf-8") as f:
        return json.load(f)["harnesses"]


def harness(hid: str) -> dict:
    h = next((x for x in catalog() if x["id"] == hid), None)
    if not h:
        raise HarnessError(f"ассистента «{hid}» нет в каталоге")
    return h


def config_path(h: dict) -> str:
    tpl = h.get("config_" + platform_key()) or h["config"]
    return expand(tpl)


def bin_dirs() -> list:
    """Где ещё лежат команды ассистентов, когда панель запущена без PATH оболочки."""
    dirs = [os.path.join(home(), ".local", "bin"), os.path.join(home(), ".opencode", "bin"),
            "/opt/homebrew/bin", "/usr/local/bin", os.path.join(home(), ".npm-global", "bin"),
            os.path.join(home(), ".bun", "bin"), os.path.join(home(), ".cargo", "bin")]
    dirs += sorted(glob.glob(os.path.join(home(), ".nvm", "versions", "node", "*", "bin")),
                   reverse=True)
    if platform_key() == "windows":
        dirs.append(os.path.join(os.environ.get("APPDATA", ""), "npm"))
    return dirs


def find_bin(names: list) -> str:
    for n in names or []:
        hit = shutil.which(n)
        if hit:
            return hit
        for d in bin_dirs():
            for cand in (os.path.join(d, n), os.path.join(d, n + ".cmd"), os.path.join(d, n + ".exe")):
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return ""


def bin_version(path: str, args: list) -> str:
    try:
        p = subprocess.run([path, *(args or ["--version"])], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=VERSION_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    line = ((p.stdout or "").strip() or (p.stderr or "").strip()).splitlines()
    return line[0][:80] if line else ""


# ------------------------------------------------------------------ запись Авроры

def server_command(kit: str = "") -> tuple:
    """(команда, аргументы) сервера Авроры: все проекты машины, проект — в вызове."""
    kit = kit or str(HERE.parent)
    return sys.executable, [os.path.join(kit, "scripts", "aurora_mcp.py"), "--all"]


def entry(h: dict, command: str, args: list) -> dict:
    kind, extra = h.get("entry", "standard"), dict(h.get("entry_extra") or {})
    if kind == "vscode":
        return {"type": "stdio", "command": command, "args": args, **extra}
    if kind == "opencode":
        return {"type": "local", "command": [command, *args], "enabled": True, **extra}
    if kind == "crush":
        return {"type": "stdio", "command": command, "args": args, **extra}
    if kind == "zed":
        return {"command": command, "args": args, "env": {}, "enabled": True, **extra}
    if kind == "chatbox":
        import uuid
        return {"id": str(uuid.uuid4()), "name": NAME, "enabled": True,
                "transport": {"type": "stdio", "command": command, "args": args, **extra}}
    return {"command": command, "args": args, **extra}


def snippet(h: dict, command: str, args: list) -> str:
    """То, что ляжет в файл ассистента, — чтобы человек видел запись до кнопки."""
    fmt = h.get("format")
    if fmt == "toml":
        return toml_block(h, command, args).strip()
    if fmt == "continue":
        return continue_file(command, args).strip()
    if fmt == "yaml":
        return f"{h['key']}:\n" + yaml_block(h, command, args, "  ").rstrip()
    if fmt == "chatbox":
        return json.dumps({"settings": {"mcp": {"servers": ["…", entry(h, command, args)]}}},
                          ensure_ascii=False, indent=2)
    return json.dumps({h["key"]: {NAME: entry(h, command, args)}}, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------ JSON с комментариями

def strip_jsonc(text: str) -> str:
    """JSONC → JSON: без комментариев и висячих запятых; строки не трогаются."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    clean = "".join(out)
    # висячие запятые — уже без комментариев и вне строк
    res, in_str, i = [], False, 0
    while i < len(clean):
        c = clean[i]
        if in_str:
            res.append(c)
            if c == "\\" and i + 1 < len(clean):
                res.append(clean[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            res.append(c)
        elif c == ",":
            j = i + 1
            while j < len(clean) and clean[j] in " \t\r\n":
                j += 1
            if j < len(clean) and clean[j] in "}]":
                i += 1
                continue
            res.append(c)
        else:
            res.append(c)
        i += 1
    return "".join(res)


def load_jsonc(text: str):
    return json.loads(strip_jsonc(text)) if text.strip() else {}


def _scan(text: str):
    """Поток (позиция, символ, глубина) вне строк и комментариев — для вставки в текст."""
    i, n, depth, in_str = 0, len(text), 0, False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            yield i, c, depth
            in_str = True
            i += 1
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "{[":
            depth += 1
            yield i, c, depth
        elif c in "}]":
            yield i, c, depth
            depth -= 1
        else:
            yield i, c, depth
        i += 1


def _string_at(text: str, i: int) -> tuple:
    """Строка JSON с позиции `i` (там кавычка) → (значение, позиция за ней)."""
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == '"':
            break
        j += 1
    return json.loads(text[i:j + 1]), j + 1


def _next_meaningful(text: str, i: int) -> int:
    for pos, c, _d in _scan_from(text, i):
        if not c.isspace():
            return pos
    return -1


def _scan_from(text: str, start: int):
    sub = text[start:]
    for pos, c, d in _scan(sub):
        yield pos + start, c, d


def insert_json(text: str, key: str, name: str, value: dict) -> str:
    """Вставить `"name": value` в объект верхнего уровня `key` — правкой текста."""
    body = json.dumps(value, ensure_ascii=False, indent=2)
    root = next((pos for pos, c, _d in _scan(text) if c == "{"), -1)
    if root < 0:
        raise HarnessError("файл не похож на объект JSON")
    target = -1
    for pos, c, d in _scan(text):
        if c == '"' and d == 1:
            k, after = _string_at(text, pos)
            colon = _next_meaningful(text, after)
            if k == key and colon >= 0 and text[colon] == ":":
                brace = _next_meaningful(text, colon + 1)
                if brace >= 0 and text[brace] == "{":
                    target = brace
                    break
                raise HarnessError(f"«{key}» в файле — не объект: правка вручную")
    if target >= 0:
        inner = "\n".join("    " + l if i else l for i, l in enumerate(body.splitlines()))
        nxt = _next_meaningful(text, target + 1)
        if nxt >= 0 and text[nxt] == "}":
            # Пустой объект `{}`: закрывающая скобка — на свою строку, как у человека.
            return text[:target + 1] + f'\n    "{name}": {inner}\n  ' + text[nxt:]
        return text[:target + 1] + f'\n    "{name}": {inner},' + text[target + 1:]
    inner = "\n".join("    " + l if i else l for i, l in enumerate(body.splitlines()))
    nxt = _next_meaningful(text, root + 1)
    sep = "" if nxt >= 0 and text[nxt] == "}" else ","
    block = f'\n  "{key}": {{\n    "{name}": {inner}\n  }}{sep}'
    return text[:root + 1] + block + text[root + 1:]


# ------------------------------------------------------------------ TOML и YAML

def toml_str(v: str) -> str:
    return json.dumps(v, ensure_ascii=False)


def toml_block(h: dict, command: str, args: list) -> str:
    lines = ["", f"[{h['key']}.{NAME}]", f"command = {toml_str(command)}",
             "args = [" + ", ".join(toml_str(a) for a in args) + "]"]
    for k, v in (h.get("entry_extra") or {}).items():
        lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def yaml_block(h: dict, command: str, args: list, indent: str) -> str:
    """Запись под ключом YAML — строки в стиле JSON: их понимает любой YAML-разборщик."""
    i2 = indent * 2
    rows = [f"{indent}{NAME}:"]
    if h.get("entry") == "goose":
        rows += [f"{i2}name: {NAME}", f"{i2}type: stdio", f"{i2}cmd: {toml_str(command)}",
                 f"{i2}args: [" + ", ".join(toml_str(a) for a in args) + "]",
                 f"{i2}enabled: true"]
    else:
        rows += [f"{i2}command: {toml_str(command)}",
                 f"{i2}args: [" + ", ".join(toml_str(a) for a in args) + "]"]
    rows += [f"{i2}{k}: {json.dumps(v)}" for k, v in (h.get("entry_extra") or {}).items()]
    return "\n".join(rows) + "\n"


def continue_file(command: str, args: list) -> str:
    return ("name: Aurora\nversion: 0.0.1\nschema: v1\nmcpServers:\n"
            f"  - name: {NAME}\n    command: {toml_str(command)}\n"
            "    args: [" + ", ".join(toml_str(a) for a in args) + "]\n")


def yaml_has(text: str, key: str) -> bool:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:\s*(#.*)?$", line):
            for sub in lines[i + 1:]:
                if sub.strip() and not sub.startswith((" ", "\t")) and not sub.startswith("#"):
                    break
                if re.match(rf"^\s+{NAME}:\s*(#.*)?$", sub):
                    return True
    return False


def insert_yaml(text: str, h: dict, command: str, args: list) -> str:
    key = h["key"]
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = re.match(rf"^{re.escape(key)}:\s*(.*?)\s*$", line.rstrip("\n"))
        if not m:
            continue
        rest = m.group(1)
        if rest and not rest.startswith("#") and rest not in ("{}", "null", "~"):
            raise HarnessError(f"«{key}» в файле записан одной строкой ({rest[:40]}) — правка вручную")
        indent = "  "
        for sub in lines[i + 1:]:
            if sub.strip() and not sub.lstrip().startswith("#"):
                got = re.match(r"^(\s+)\S", sub)
                if got:
                    indent = got.group(1)
                break
        head = f"{key}:\n" if rest in ("{}", "null", "~") else line
        if not head.endswith("\n"):
            head += "\n"
        return "".join(lines[:i]) + head + yaml_block(h, command, args, indent) + "".join(lines[i + 1:])
    tail = "" if not text or text.endswith("\n") else "\n"
    return text + tail + f"{key}:\n" + yaml_block(h, command, args, "  ")


# ------------------------------------------------------------------ есть ли Аврора

def has_aurora(h: dict, path: str):
    """True/False — подключена ли; None — файла нет или он не разбирается."""
    if not os.path.isfile(path):
        return False            # файла нет — Авроры нет; подключение его создаст
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    fmt = h.get("format")
    if fmt == "continue":
        return True
    if fmt == "toml":
        return bool(re.search(rf"(?m)^\[{re.escape(h['key'])}\.{NAME}\]", text)) or "aurora_mcp.py" in text
    if fmt == "yaml":
        return yaml_has(text, h["key"]) or "aurora_mcp.py" in text
    try:
        data = load_jsonc(text)
    except ValueError:
        return None
    if fmt == "chatbox":
        servers = chatbox_servers(data)
        if servers is None:
            return None
        return any(isinstance(s, dict) and (s.get("name") == NAME or "aurora_mcp.py" in
                                            json.dumps(s, ensure_ascii=False)) for s in servers)
    servers = data.get(h["key"]) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return False
    return NAME in servers or any("aurora_mcp.py" in json.dumps(v, ensure_ascii=False)
                                  for v in servers.values())


def valid(h: dict, path: str) -> str:
    """Пусто — файл после правки разбирается; иначе причина."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    fmt = h.get("format")
    if fmt in ("json", "chatbox"):
        try:
            load_jsonc(text)
        except ValueError as e:
            return f"JSON не разбирается: {e}"
    elif fmt == "toml":
        try:
            import tomllib
        except ImportError:
            return ""
        try:
            tomllib.loads(text)
        except Exception as e:  # noqa: BLE001
            return f"TOML не разбирается: {e}"
    if has_aurora(h, path) is not True:
        return "запись Авроры после правки не находится"
    return ""


# ------------------------------------------------------------------ копии

def _manifest(hid: str) -> Path:
    return backups_root() / hid / "backups.json"


def list_backups(hid: str) -> list:
    try:
        rows = json.loads(_manifest(hid).read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _save_manifest(hid: str, rows: list) -> None:
    p = _manifest(hid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def backup(hid: str, path: str, why: str) -> dict:
    """Копия файла ассистента до правки. Файла не было — отметка «создан», возврат удалит его."""
    stamp = utc_slug("%Y%m%d-%H%M%S")
    taken = {r["stamp"] for r in list_backups(hid)}
    # Две копии в одну секунду («подключить», сразу «вернуть») получали одно имя, и копия
    # «перед возвратом» затирала копию оригинала: возврат клал обратно правленый файл.
    base, n = stamp, 2
    while stamp in taken or (backups_root() / hid / f"{stamp}__{os.path.basename(path)}").exists():
        stamp, n = f"{base}-{n}", n + 1
    row = {"stamp": stamp, "path": path, "why": why, "created": not os.path.exists(path)}
    if not row["created"]:
        dst = backups_root() / hid / f"{stamp}__{os.path.basename(path)}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)
        row["backup"] = str(dst)
    rows = list_backups(hid) + [row]
    _save_manifest(hid, rows[-30:])
    return row


def restore(hid: str, stamp: str = "") -> dict:
    """Вернуть файл ассистента из копии (по умолчанию — последней). Нынешний — тоже в копию."""
    rows = list_backups(hid)
    row = next((r for r in reversed(rows) if not stamp or r["stamp"] == stamp), None)
    if not row:
        raise HarnessError("копий нет — возвращать нечего")
    path = row["path"]
    if os.path.exists(path):
        backup(hid, path, "перед возвратом из копии")
    if row.get("created"):
        if os.path.exists(path):
            os.remove(path)
        return {"ok": True, "path": path, "restored": "файл удалён — до правки его не было"}
    if not os.path.isfile(row.get("backup", "")):
        raise HarnessError(f"файла копии нет: {row.get('backup')}")
    shutil.copy2(row["backup"], path)
    return {"ok": True, "path": path, "restored": f"файл возвращён из копии {row['stamp']}"}


# ------------------------------------------------------------------ найти и подключить

def detect(versions: bool = True) -> list:
    """Ассистенты из каталога: найден ли, версия, файл настройки, подключена ли Аврора."""
    command, args = server_command()
    rows = []
    for h in catalog():
        b = find_bin(h.get("bins") or [])
        marks = [expand(p) for p in h.get("detect") or []]
        cfg = config_path(h)
        found = bool(b) or any(os.path.exists(p) for p in marks) or os.path.exists(cfg)
        rows.append({"id": h["id"], "name": h["name"], "found": found, "bin": b,
                     "version": "", "config": cfg, "config_exists": os.path.exists(cfg),
                     "aurora": has_aurora(h, cfg) if found else None,
                     "backups": len(list_backups(h["id"])), "note": h.get("note", ""),
                     "format": h.get("format", "json"),
                     "snippet": snippet(h, command, args)})
    if versions:
        def ver(row, h):
            if row["bin"]:
                row["version"] = bin_version(row["bin"], h.get("version_args") or [])
        threads = []
        for row, h in zip(rows, catalog()):
            if row["found"] and row["bin"]:
                t = threading.Thread(target=ver, args=(row, h), daemon=True)
                t.start()
                threads.append(t)
        for t in threads:
            t.join(timeout=VERSION_TIMEOUT + 2)
    return rows


def add(hid: str) -> dict:
    """Подключить Аврору к ассистенту: копия → правка → проверка; не прошла — возврат."""
    h = harness(hid)
    command, args = server_command()
    path = config_path(h)
    state = has_aurora(h, path)
    if state is True:
        return {"ok": True, "path": path, "done": "Аврора уже подключена — файл не трогал"}
    if state is None and os.path.exists(path):
        raise HarnessError(f"файл настройки не разбирается — правка вручную: {path}")
    if h.get("process") and app_running(h["process"]):
        # Приложение держит настройки в памяти и при выходе переписывает файл целиком:
        # запись, сделанная при открытом окне, пропала бы молча.
        raise HarnessError(f"{h['name']} открыт — закройте его и нажмите ещё раз: при выходе "
                           "он перепишет файл настроек, и запись пропадёт")
    row = backup(hid, path, "перед подключением Авроры")
    try:
        if h.get("cli_add") and find_bin(h.get("bins") or []):
            how = add_by_cli(h, command, args)
        else:
            how = add_by_edit(h, path, command, args)
        why = valid(h, path)
        if why:
            raise HarnessError(why)
    except Exception as e:  # noqa: BLE001 — любая неудача возвращает файл как был
        if row.get("created"):
            if os.path.exists(path):
                os.remove(path)
        elif row.get("backup"):
            shutil.copy2(row["backup"], path)
        raise HarnessError(f"не подключил ({e}); файл возвращён как был") from None
    return {"ok": True, "path": path, "done": how, "backup": row.get("backup", ""),
            "note": h.get("note", "")}


def add_by_cli(h: dict, command: str, args: list) -> str:
    b = find_bin(h.get("bins") or [])
    argv = []
    for part in h["cli_add"]:
        if part == "{args}":
            argv += args
        else:
            argv.append(part.replace("{bin}", b).replace("{name}", NAME)
                        .replace("{command}", command))
    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    if p.returncode != 0:
        raise HarnessError(((p.stderr or p.stdout or "").strip() or f"код {p.returncode}")[:300])
    return f"командой {os.path.basename(b)} mcp add"


def add_by_edit(h: dict, path: str, command: str, args: list) -> str:
    fmt = h.get("format")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if fmt == "continue":
        Path(path).write_text(continue_file(command, args), encoding="utf-8")
        return "создан свой файл настройки"
    text = Path(path).read_text(encoding="utf-8") if os.path.isfile(path) else ""
    if fmt == "chatbox":
        new = chatbox_insert(text, entry(h, command, args))
    elif fmt == "toml":
        new = text + ("" if not text or text.endswith("\n") else "\n") + toml_block(h, command, args)
    elif fmt == "yaml":
        new = insert_yaml(text, h, command, args)
    else:
        if not text.strip():
            new = json.dumps({h["key"]: {NAME: entry(h, command, args)}}, ensure_ascii=False,
                             indent=2) + "\n"
        else:
            new = insert_json(text, h["key"], NAME, entry(h, command, args))
    tmp = path + ".aurora-tmp"
    Path(tmp).write_text(new, encoding="utf-8")
    os.replace(tmp, path)
    return "запись добавлена в файл настройки"


# ------------------------------------------------------------------ Chatbox

def chatbox_servers(data):
    """Список серверов Chatbox (`settings.mcp.servers`); None — файл не того вида."""
    if not isinstance(data, dict):
        return None
    mcp = (data.get("settings") or {}).get("mcp") if isinstance(data.get("settings"), dict) else None
    if mcp is None:
        return []
    servers = mcp.get("servers") if isinstance(mcp, dict) else None
    return servers if isinstance(servers, list) else ([] if servers is None else None)


def chatbox_insert(text: str, server: dict) -> str:
    """Chatbox хранит серверы списком внутри всех своих настроек — запись дописывается в
    конец списка, остальное остаётся как было; отступ — табуляцией, как пишет сам Chatbox."""
    data = json.loads(text) if text.strip() else {}
    settings = data.setdefault("settings", {})
    mcp = settings.setdefault("mcp", {})
    servers = mcp.setdefault("servers", [])
    mcp.setdefault("enabledBuiltinServers", [])
    servers.append(server)
    indent = "\t" if text.startswith("{\n\t") or not text.strip() else 2
    return json.dumps(data, ensure_ascii=False, indent=indent)


def app_running(names: list) -> bool:
    """Запущено ли приложение (по имени процесса)."""
    try:
        if platform_key() == "windows":
            for n in names:
                p = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {n}.exe"], capture_output=True,
                                   text=True, timeout=10)
                if f"{n}.exe".lower() in (p.stdout or "").lower():
                    return True
            return False
        return any(subprocess.run(["pgrep", "-x", n], capture_output=True, timeout=10).returncode == 0
                   for n in names)
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP Авроры в ассистентах машины")
    ap.add_argument("--list", action="store_true", help="какие ассистенты есть и где Аврора")
    ap.add_argument("--add", metavar="ID", help="подключить Аврору к ассистенту")
    ap.add_argument("--restore", metavar="ID", help="вернуть файл ассистента из последней копии")
    ap.add_argument("--json", action="store_true", help="вывод JSON-ом")
    a = ap.parse_args()
    try:
        if a.add:
            out = add(a.add)
        elif a.restore:
            out = restore(a.restore)
        else:
            out = {"harnesses": detect()}
    except HarnessError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False) if a.json
              else f"✗ {e}")
        return 1
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif "harnesses" in out:
        for r in out["harnesses"]:
            if not r["found"]:
                continue
            mark = {True: "✓ Аврора подключена",
                    False: "— Авроры нет" if r["config_exists"] else "— Авроры нет (файл будет создан)",
                    None: "? файл настройки не разбирается"}[r["aurora"]]
            print(f"{r['name']:<22} {r['version'][:30]:<30} {mark} · {r['config']}")
    else:
        print("✓ " + (out.get("done") or out.get("restored") or "") + f" · {out.get('path', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
