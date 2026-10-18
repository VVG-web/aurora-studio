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
- **вердикт** (`check_command`, `check_steps`) — доступно ли нужное сейчас, и если нет — что именно и почему; решение
  «что с этим делать» принимает политика (`policy_for`, `decide`): `fail_fast` — отказать, `partial` — пропустить,
  `queue` — дождаться и идти целиком, `partial_queue` (по умолчанию для автоматических прогонов) — доступное идёт сразу,
  недоступное откладывается и повторяется после восстановления. Ждать можно без предела (`wait_restored`): маршрут
  доводит дело до конца, пока его не остановит человек;
- **провайдеры моделей** (`providers_down`, `note_failure`) — кого не трогать, пока его профиль или интернет недоступны:
  роутер модели пропускает такого провайдера и не садит его в карантин за чужую беду.

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
import threading
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
KNOWN_VARIANTS = ("parse_downloaded",)       # варианты «работает без связи», которым есть подпись в закладке «Команды»
_TRUE = ("true", "yes", "да", "1", "on")
_FALSE = ("false", "no", "нет", "0", "off")
_NAME_OK = re.compile(r"^[A-Za-z0-9_.-]+$")


# ------------------------------------------------------------------ блок source_access: в конфиге проекта

_HEAD_RE = re.compile(r"""^(?:"source_access"|'source_access'|source_access)[ \t]*:""")
_SAVE_LOCK = threading.Lock()       # запись блока: прочитать, сверить слепок и записать — одним ходом, а не вперемешку
_P_DUP = f"{ACCESS_KEY}: блок записан"                                 # начала двух проблем, которые страница показывает кодом
_P_INLINE = f"{ACCESS_KEY}: значение записано в одну строку"


def _spans(lines: list) -> list:
    """[(первая строка, строка после последней)] всех блоков `source_access:` верхнего уровня (строки без окончаний).

    Блок — строка ключа и всё вложенное под ней; пустые строки и комментарии без отступа в конце блока ему
    не принадлежат. Закомментированный пример блоком не считается; ключ в кавычках или с пробелом перед «:» — тоже блок."""
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line[:1] in ("", " ", "\t", "#") or not _HEAD_RE.match(line):
            i += 1
            continue
        last = i
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip() or nxt[0] == "#":
                continue
            if nxt[0] in (" ", "\t"):
                last = j
            else:
                break
        out.append((i, last + 1))
        i = last + 1
    return out


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
    """Значение в двойных кавычках с экранированием `\\` и `"`: любое имя записывается и читается обратно как есть."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split_key(line: str):
    """«  "Имя (старое)": значение» → (ключ, остаток) с учётом кавычек у ключа."""
    s = line.strip()
    if s[:1] in "\"'":
        q = s[0]
        i = 1
        while i < len(s):
            ch = s[i]
            if q == '"' and ch == "\\":
                i += 2
                continue
            if q == "'" and ch == "'" and s[i + 1:i + 2] == "'":
                i += 2
                continue
            if ch == q:
                break
            i += 1
        if i >= len(s) or s[i + 1:i + 2] != ":":
            return None, ""
        return _unquote(s[:i + 1]), s[i + 2:]
    key, sep, rest = s.partition(":")
    return (key.strip(), rest) if sep else (None, "")


def parse_access(config_text: str) -> tuple:
    """(режимы доступа, проблемы) из блока `source_access:` → {id источника: {mode, vpn, internet, offline_cache}}.

    Читает построчно, без YAML-библиотеки; негодное пропускает и называет в проблемах, а не падает на ручной правке."""
    lines = str(config_text).removeprefix(BOM).replace("\r\n", "\n").split("\n")
    spans = _spans(lines)
    if not spans:
        return {}, []
    found: dict = {}
    problems: list = []
    if len(spans) > 1:
        problems.append(f"{_P_DUP} {len(spans)} раза — читается первый, лишние при сохранении уберутся")
    first, end = spans[0]
    head = lines[first]
    inline = _unquote(head[_HEAD_RE.match(head).end():])
    if inline and inline not in ("{}", "~", "null"):
        problems.append(f"{_P_INLINE} с ключом — читаются только вложенные строки")
    base = sid = None
    for raw in lines[first + 1:end]:
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
            if not value or CN.valid_name(value):
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


def lossy_of(config_text: str) -> list:
    """Что пропадёт, если блок `source_access:` переписать из разобранного: комментарии внутри блока, лишние поля,
    запись в одну строку, второй блок. Пусто — переписывание ничего не теряет (правит страница, а не человек)."""
    lines = str(config_text).removeprefix(BOM).replace("\r\n", "\n").split("\n")
    spans = _spans(lines)
    if not spans:
        return []
    notes = list(parse_access(config_text)[1])
    if any("#" in ln for a, b in spans for ln in lines[a:b]):
        notes.append(f"{ACCESS_KEY}: в блоке есть комментарии — при сохранении они пропадут")
    return notes


def lossy_kinds(config_text: str) -> list:
    """Те же потери кодами для страницы: `comments`, `inline` (запись в одну строку), `duplicate` (второй блок),
    `skipped` (строки и поля, которых движок не читает). Подписи к кодам — в каталоге строк страницы."""
    lines = str(config_text).removeprefix(BOM).replace("\r\n", "\n").split("\n")
    spans = _spans(lines)
    if not spans:
        return []
    problems = parse_access(config_text)[1]
    out = []
    if any(p.startswith(_P_DUP) for p in problems):
        out.append("duplicate")
    if any(p.startswith(_P_INLINE) for p in problems):
        out.append("inline")
    if any("#" in ln for a, b in spans for ln in lines[a:b]):
        out.append("comments")
    if any(not p.startswith((_P_DUP, _P_INLINE)) for p in problems):
        out.append("skipped")
    return out


def block_digest(found: dict) -> str:
    """Слепок режимов доступа; блока нет или он пуст — пустая строка (страница, открытая без блока, шлёт её же)."""
    return CN.digest(found) if found else ""


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
            if not isinstance(vpn, str) or not CN.valid_name(vpn.strip()):
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
        if row.get("mode") and row["mode"] not in ("vpn", "mixed"):
            row.pop("vpn", None)         # имя профиля нужно только режимам с VPN: в других оно — остаток прежнего выбора
        if row:
            out[sid] = row
    return out, errors


def _block_lines(access: dict) -> list:
    if not access:
        return []
    out = [ACCESS_KEY + ":"]
    for sid in sorted(access):
        out.append(f"  {_quote(sid)}:")
        row = access[sid]
        if "mode" in row:
            out.append(f'    mode: "{row["mode"]}"')
        if row.get("vpn"):
            out.append(f"    vpn: {_quote(row['vpn'])}")
        for k in ("internet", "offline_cache"):
            if k in row:
                out.append(f'    {k}: "{"true" if row[k] else "false"}"')
    return out


def _keep_ends(text: str) -> list:
    """Строки текста вместе с их окончаниями (`\\n` или `\\r\\n`); последняя может быть без окончания."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def write_access(config_text: str, access: dict) -> str:
    """Текст конфига с переписанным блоком `source_access:`; всё остальное — как было, байт в байт (окончания строк,
    метка порядка байтов и отсутствие перевода строки в конце сохраняются). Нет блока — добавляется в конец; пустые
    режимы убирают блок; лишние (повторные) блоки убираются."""
    bom = BOM if config_text.startswith(BOM) else ""
    body = config_text[len(bom):]
    keep = _keep_ends(body)
    spans = _spans([ln.rstrip("\r\n") for ln in keep])
    eol = "\r\n" if "\r\n" in body else "\n"
    block = [ln + eol for ln in _block_lines(access)]
    if not spans:
        if not block:
            return config_text
        out = body
        if out and not out.endswith("\n"):
            out += eol
        if out.strip() and not re.search(r"\n[ \t]*\r?\n$", out):
            out += eol
        return bom + out + "".join(block)
    a, b = spans[0]
    new = keep[:a] + block
    pos = b
    for sa, sb in spans[1:]:
        new += keep[pos:sa]
        pos = sb
    new += keep[pos:]
    if block and len(spans) == 1 and b == len(keep) and not keep[b - 1].endswith("\n"):
        new[a + len(block) - 1] = block[-1].rstrip("\r\n")          # у файла не было перевода строки в конце — и не будет
    if not block and a > 0 and not new[a - 1].strip():
        if a < len(new):
            if not new[a].strip():
                del new[a]                                          # две пустые строки сошлись — остаётся одна
        else:
            del new[a - 1]                                          # блок был последним: его отбивка уходит вместе с ним
    return bom + "".join(new)


def access_digest(project: str) -> str:
    """Слепок блока `source_access:` как его видит движок — страница шлёт его при записи (`expect`)."""
    return block_digest(parse_access(_config_text(project))[0])


def _config_text(project: str) -> str:
    try:
        with open(os.path.join(project, CONFIG_FILE), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def save_access(project: str, access: dict, expect=None, replace_lossy: bool = False) -> dict:
    """Записать блок `source_access:` проекта → {ok, digest} либо {ok: False, code, error[, conflict, lossy]}.

    Чужие разделы конфига, комментарии и окончания строк остаются как были. `expect` — слепок блока, с которым страница
    его открыла: блок за это время менялся — отказ `conflict`. В блоке есть то, что переписывание потеряет (комментарии,
    лишние поля, запись в одну строку) — отказ `lossy` со списком, пока человек не подтвердит `replace_lossy`.
    Запись неделимая; конфиг не в UTF-8 не трогается."""
    clean, errors = clean_access(access)
    if errors:
        return {"ok": False, "code": errors[0]["code"], "error": errors[0]["text"], "errors": errors}
    path = os.path.join(project, CONFIG_FILE)
    with _SAVE_LOCK:
        try:
            with open(path, encoding="utf-8", newline="") as f:
                old = f.read()
        except FileNotFoundError:
            return {"ok": False, "code": "no_config", "error": "в проекте нет aurora.config.yaml", "errors": []}
        except UnicodeDecodeError:
            return {"ok": False, "code": "not_utf8", "error": "aurora.config.yaml не в кодировке UTF-8 — файл не тронут",
                    "errors": []}
        except OSError as e:
            return {"ok": False, "code": "write", "failed": True, "errors": [],
                    "error": f"не удалось прочитать aurora.config.yaml: {e.strerror or e}"}
        if expect is not None and expect != block_digest(parse_access(old)[0]):
            return {"ok": False, "conflict": True, "code": "conflict", "errors": [],
                    "error": "режимы доступа изменились на диске с момента открытия страницы"}
        new = write_access(old, clean)
        if new != old:
            lossy = lossy_of(old)
            if lossy and not replace_lossy:
                return {"ok": False, "code": "lossy", "lossy": lossy_kinds(old), "errors": [],
                        "error": "в блоке source_access есть то, что сохранение перепишет: " + "; ".join(lossy[:3])}
            try:
                _write_exact(path, new)
            except OSError as e:
                return {"ok": False, "code": "write", "failed": True, "errors": [],
                        "error": f"не удалось записать aurora.config.yaml: {e.strerror or e}"}
    return {"ok": True, "digest": block_digest(parse_access(new)[0])}


def _write_exact(path: str, text: str) -> None:
    """Записать целиком или не трогать файл, без перевода строк (`\\r\\n` остаётся `\\r\\n`); права прежнего файла
    сохраняются. Временный файл у каждой записи свой: две записи разом друг другу не мешают."""
    import shutil
    import tempfile
    from aurora_common import replace_file
    real = Path(os.path.realpath(path))
    fd, name = tempfile.mkstemp(dir=str(real.parent), prefix=real.name + ".", suffix=".aurora-tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
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
            "prefix": str((manifest.get("auth") or {}).get("env_prefix") or "") if isinstance(manifest.get("auth"), dict) else "",
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
            "digest": block_digest(found), "problems": problems, "lossy": lossy_kinds(_config_text(project))}


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
    items = data.get("commands") if isinstance(data, dict) else None
    for cmd, row in (items.items() if isinstance(items, dict) else []):
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


# ------------------------------------------------------------------ вердикт: можно ли начинать

NET_POLICY_ENV = "AURORA_NET_POLICY"      # политика автоматического прогона для процессов, которые он запускает
CHOICES = ("partial", "queue", "cancel")   # выбор человека в диалоге запуска из панели


def kit_policy(svc=None) -> str:
    """Политика автоматических прогонов без связи из настройки кита; умолчание — `partial_queue`."""
    try:
        svc = svc or CN.service()
        auto = (svc.config().get("policy") or {}).get("auto")
    except Exception:  # noqa: BLE001 — нет настройки: умолчание
        auto = None
    return auto if auto in CN.POLICIES else CN.POLICY_DEFAULT


def policy_for(override: str = "", svc=None) -> str:
    """Политика этого прогона: переопределение задания (Cron, маршрут) или окружение процесса, иначе — настройка кита."""
    for cand in (override, os.environ.get(NET_POLICY_ENV, "")):
        if cand in CN.POLICIES:
            return cand
    return kit_policy(svc)


def decide(policy: str, verdict: dict) -> str:
    """Что делать с делом, которому не хватает связи → run | fail | skip | defer | wait.

    `fail_fast` — отказать сразу; `partial` — пропустить (доступное идёт); `queue` — ждать восстановления и идти целиком;
    `partial_queue` — доступное сразу, недоступное отложить и повторить после восстановления."""
    if verdict.get("ok", True):
        return "run"
    return {"fail_fast": "fail", "partial": "skip", "queue": "wait", "partial_queue": "defer"}.get(policy, "defer")


def _row_state(rows: list, key: str, value: str) -> str:
    for r in rows:
        if r.get(key) == value:
            return r.get("state", "unknown")
    return "unknown"


def evaluate(req: dict, snap: dict) -> dict:
    """Требования команды против сводки связи → {ok, blocked[], state}. Блокирует только известное «недоступно»:
    `unknown` (проверок не было, у интернета нет probe) дело не останавливает."""
    out = {"ok": True, "blocked": [], "state": snap.get("state", "unknown"), "enabled": bool(snap.get("enabled"))}
    if not snap.get("enabled"):
        return out
    profiles = snap.get("profiles") or []
    net = snap.get("internet") or {}
    net_down = bool(net.get("monitored")) and net.get("state") == "offline"
    seen: set = set()

    def block(code: str, **kw) -> None:
        key = (code, kw.get("name", ""), kw.get("source", ""))
        if key not in seen:
            seen.add(key)
            out["blocked"].append({"code": code, **kw})

    for s in req.get("sources") or []:
        if s.get("profile") and _row_state(profiles, "name", s["profile"]) == "offline":
            # Из кэша источник доступен, но синк без связи не идёт: кэш выручает только команду, которая умеет без сети.
            if not (s.get("offline_cache") and req.get("offline_capable")):
                block("profile_offline", name=s["profile"], source=s["id"])
        if s.get("internet") and net_down:
            block("internet_offline", name=CN.INTERNET, source=s["id"])
    if req.get("internet") and net_down:
        block("internet_offline", name=CN.INTERNET)
    for name in req.get("profiles") or []:
        if _row_state(profiles, "name", name) == "offline":
            block("profile_offline", name=name)
    if req.get("llm"):
        used = req.get("providers_used")
        rows = [r for r in (snap.get("providers") or []) if used is None or r["id"] in used]
        if rows and all(r["state"] == "offline" for r in rows):
            block("llm_down", name="llm", providers=[r["id"] for r in rows],
                  why=sorted({r.get("why", "") for r in rows if r.get("why")}),
                  profiles=sorted({r["profile"] for r in rows if r.get("profile")}))
    out["ok"] = not out["blocked"]
    return out


def reason_line(r: dict) -> str:
    """Причина блокировки строкой для консоли и журнала."""
    who = f" (нужен источнику «{r['source']}»)" if r.get("source") else ""
    if r["code"] == "profile_offline":
        return f"{r['name']} недоступен{who}"
    if r["code"] == "internet_offline":
        return f"интернет недоступен{who}"
    if r["code"] == "llm_down":
        parts = []
        if r.get("profiles"):
            parts.append("профили не отвечают: " + ", ".join(r["profiles"]))
        if "provider_check" in (r.get("why") or []):
            parts.append("проверка ручки не прошла")
        if "internet_offline" in (r.get("why") or []):
            parts.append("нет интернета")
        return "все провайдеры моделей недоступны" + (" — " + "; ".join(parts) if parts else "")
    return r["code"]


def blocked_text(cmd: str, verdict: dict) -> str:
    """«Не запущено: …» — одна строка причины, которую видит человек вместо таймаута."""
    return f"{cmd}: нужное недоступно — " + "; ".join(reason_line(r) for r in verdict["blocked"])


def needed_profiles(req: dict) -> list:
    return sorted(set(req.get("profiles") or []) | set(req.get("llm_profiles") or []))


def check_requirements(req: dict, svc=None, budget: float = CN.PRELAUNCH_BUDGET) -> dict:
    """Предзапуск: свежее состояние из кэша, устаревшее — перепроверка на месте (≤ бюджета), затем вердикт."""
    svc = svc or CN.service()
    if not svc.enabled:
        return {"ok": True, "blocked": [], "state": "unknown", "enabled": False}
    svc.ensure(profiles=needed_profiles(req), budget=budget)
    if req.get("llm") and "providers_used" not in req:
        req = dict(req, providers_used=[p["id"] for p in models_used(svc.kit)])
    snap = svc.snapshot({"profiles": needed_profiles(req), "providers": req.get("providers_used"),
                         "internet": bool(req.get("internet"))})
    return evaluate(req, snap)


def check_command(cmd: str, project: str, svc=None, deps: dict | None = None, budget: float = CN.PRELAUNCH_BUDGET) -> dict:
    """Можно ли запускать команду в проекте → {ok, blocked[], requirements}. Проверка выключена — всегда «можно»."""
    svc = svc or CN.service()
    if not svc.enabled:
        return {"ok": True, "blocked": [], "state": "unknown", "enabled": False, "cmd": cmd}
    req = command_requirements(cmd, project, deps)
    out = check_requirements(req, svc, budget)
    out.update(cmd=cmd, offline_capable=req["offline_capable"])
    return out


def merge_requirements(reqs: list) -> dict:
    """Одно требование на несколько команд (маршрут целиком): профили и источники складываются, модель нужна, если нужна любой."""
    out = {"cmd": "", "profiles": [], "internet": False, "llm": False, "llm_profiles": [], "sources": [],
           "offline_capable": all(r.get("offline_capable") for r in reqs) if reqs else True}
    for r in reqs:
        out["profiles"] = sorted(set(out["profiles"]) | set(r.get("profiles") or []))
        out["llm_profiles"] = sorted(set(out["llm_profiles"]) | set(r.get("llm_profiles") or []))
        out["internet"] = out["internet"] or bool(r.get("internet"))
        out["llm"] = out["llm"] or bool(r.get("llm"))
        out["sources"] += [s for s in r.get("sources") or [] if s not in out["sources"]]
    return out


def check_steps(cmds: list, project: str, svc=None, budget: float = CN.PRELAUNCH_BUDGET) -> list:
    """Шаги маршрута или цепочки: один предзапуск на всё, вердикт на каждый шаг → [{cmd, ok, blocked}] в порядке шагов."""
    svc = svc or CN.service()
    if not svc.enabled:
        return [{"cmd": c, "ok": True, "blocked": []} for c in cmds]
    deps = load_deps()
    srcs = source_instances(project) if project else []
    provs = models_used(svc.kit)
    reqs = {c: command_requirements(c, project, deps, srcs, provs) for c in dict.fromkeys(cmds)}
    merged = merge_requirements(list(reqs.values()))
    svc.ensure(profiles=needed_profiles(merged), budget=budget)
    snap = svc.snapshot({"profiles": needed_profiles(merged), "providers": [p["id"] for p in provs],
                         "internet": merged["internet"]})
    out = []
    for c in cmds:
        r = dict(reqs[c], providers_used=[p["id"] for p in provs])
        v = evaluate(r, snap)
        out.append({"cmd": c, **v})
    return out


def wait_restored(check, stop=None, poll: float = 20.0, on_wait=None, sleep=None, clock=None, woken=None) -> bool:
    """Ждать восстановления связи: `check()` → вердикт; идём, как только он «можно». Предела нет (маршрут доводит
    дело до конца, 1.170): выход только по `stop` (человек остановил); `woken()` — «проверить сейчас»: перепроверить,
    не дожидаясь паузы. `on_wait(вердикт, ждём_секунд)` — для журнала."""
    import time
    sleep = sleep or time.sleep
    started = (clock or time.time)()
    while True:
        v = check()
        if v.get("ok", True):
            return True
        if stop is not None and stop.is_set():
            return False
        if on_wait:
            on_wait(v, int((clock or time.time)() - started))
        step = 1.0 if poll >= 1 else max(0.01, float(poll))        # пауза дробится: «стоп» и «проверить сейчас» слышны за секунду
        for _ in range(max(1, int(poll / step))):
            if stop is not None and stop.is_set():
                return False
            if woken is not None and woken():
                break
            sleep(step)


# ------------------------------------------------------------------ синк источника: отказ с точной причиной

NET_MARK = "AURORA-NET"                  # машинная строка отказа: маршрут и расписание узнают по ней «не хватило связи»
GATE_OFF_ENV = "AURORA_NET_GATE"         # `0` — не проверять связь перед синком (стенды и отладка)


def module_of_script(script: str, project: str = ".") -> str:
    """id модуля источника, чей скрипт запуска — `script` (по `run.script` манифестов), иначе пусто."""
    try:
        import sources_registry as SR
        for mid, m in SR.installed(project).items():
            if (m.get("run") or {}).get("script") == os.path.basename(script):
                return mid
    except Exception:  # noqa: BLE001
        pass
    return ""


def sync_verdict(script: str, project: str = ".", svc=None, budget: float = CN.PRELAUNCH_BUDGET) -> dict:
    """Можно ли начинать синк источников этого скрипта: {ok, blocked[], sources[]}. Нет модуля или источника в проекте,
    проверка выключена или режим не задан — можно."""
    ok = {"ok": True, "blocked": [], "sources": []}
    svc = svc or CN.service()
    if os.environ.get(GATE_OFF_ENV) == "0" or not svc.enabled:
        return ok
    mid = module_of_script(script, project)
    if not mid:
        return ok
    mine = [s for s in source_instances(project) if s["module"] == mid]
    if not mine:
        return ok
    req = {"profiles": sorted({needs_of_source(s)["profile"] for s in mine} - {""}), "internet": False,
           "sources": [{"id": s["id"], "module": s["module"], "mode": s["mode"], "profile": needs_of_source(s)["profile"],
                        "internet": needs_of_source(s)["internet"], "offline_cache": s["offline_cache"]} for s in mine],
           "offline_capable": False}
    verdict = check_requirements(req, svc, budget)
    verdict["sources"] = [s["id"] for s in mine]
    return verdict


def sync_refusal(script: str, project: str = ".", svc=None) -> str:
    """Текст отказа синку («VPN-1 недоступен…») или пусто, если можно. Две строки: человеку и машинная `AURORA-NET {…}`."""
    v = sync_verdict(script, project, svc)
    if v["ok"]:
        return ""
    names = ", ".join(v["sources"])
    why = "; ".join(reason_line(r) for r in v["blocked"])
    marker = json.dumps({"blocked": v["blocked"], "sources": v["sources"]}, ensure_ascii=False)
    return (f"Синк источника «{names}» не запущен: {why}. Это отказ проверки соединений, а не таймаут: связь вернётся — "
            f"запустите снова (состояние — `net:status`).\n{NET_MARK} {marker}")


def parse_marker(lines) -> dict:
    """Машинная строка отказа в выводе шага → {blocked, sources} или пусто."""
    for line in lines or []:
        text = str(line)
        if text.startswith(NET_MARK + " "):
            try:
                data = json.loads(text[len(NET_MARK) + 1:])
            except ValueError:
                continue
            if isinstance(data, dict) and isinstance(data.get("blocked"), list):
                return data
    return {}


# ------------------------------------------------------------------ провайдеры моделей: кого не трогать

_DOWN: dict = {"at": 0.0, "key": None, "val": {}}
DOWN_TTL = 2.0           # секунд: решение «кого не трогать» не пересчитывается на каждый вызов модели


def providers_down(svc=None, budget: float = CN.PRELAUNCH_BUDGET, fresh: bool = False) -> dict:
    """{id провайдера: причина} для тех, кого трогать нельзя: профиль недоступен, нет интернета, проверка ручки не прошла.

    Пусто — проверка выключена или все доступны. Причина — код сводки (`profile_offline`, `internet_offline`,
    `provider_check`) плюс имя профиля. `unknown` не блокирует: недоступным провайдер считается только по известному."""
    import time
    svc = svc or CN.service()
    if not fresh and _DOWN["key"] == id(svc) and time.monotonic() - _DOWN["at"] < DOWN_TTL:
        return _DOWN["val"]
    try:
        if not svc.enabled:
            return {}
        svc.ensure(budget=budget)
        snap = svc.snapshot()
    except Exception:  # noqa: BLE001 — связь — подсказка: сбой проверки не должен останавливать работу
        return {}
    val = {r["id"]: {"why": r["why"], "profile": r.get("profile", "")}
           for r in snap.get("providers") or [] if r["state"] == "offline"}
    _DOWN.update(at=time.monotonic(), key=id(svc), val=val)
    return val


def provider_reason(down: dict) -> str:
    """Причина строкой: «VPN-1 не отвечает», «нет интернета», «проверка ручки не прошла»."""
    if down.get("why") == "profile_offline":
        return f"{down.get('profile') or 'профиль'} не отвечает"
    if down.get("why") == "internet_offline":
        return "нет интернета"
    return "проверка ручки не прошла"


def llm_outage(svc=None, fresh: bool = False) -> dict:
    """{id: причина}, если недоступны ВСЕ провайдеры, на которых работает кит; иначе пусто. Кто-то жив — партия встала не
    из-за связи, и ждать по проверке соединений нечего (прежнее ожидание вслепую остаётся)."""
    svc = svc or CN.service()
    down = providers_down(svc, fresh=fresh)
    if not down:
        return {}
    used = [p["id"] for p in models_used(getattr(svc, "kit", None))]
    if not used or any(pid not in down for pid in used):
        return {}
    return {pid: down[pid] for pid in used}


def wait_llm_back(cap: float, poll: float = 15.0, say=None, svc=None) -> bool:
    """Ждать, пока вернётся хоть один провайдер моделей, но не дольше момента `cap` (unix-секунды) → True, если ждали.

    Нет известной причины (проверка выключена, провайдеры не отмечены недоступными) — False: вызывающий ждёт по-старому."""
    import time
    out = llm_outage(svc, fresh=True)
    if not out:
        return False
    if say:
        why = "; ".join(f"{pid}: {provider_reason(d)}" for pid, d in out.items())
        say(f"все провайдеры моделей недоступны по проверке соединений ({why}) — жду восстановления, "
            f"перепроверяю каждые {int(poll)} с")
    wait_restored(lambda: {"ok": time.time() >= cap or not llm_outage(svc, fresh=True)}, poll=poll)
    return True


def watch_state(project: str, svc=None, budget: float = CN.PRELAUNCH_BUDGET) -> dict:
    """Связь для сторожа шагов (`cockpit/watchdog.py`) по проверке соединений: {known, ok, down[], covered[]}.

    `known` — проверка включена и по нужному делу проекта уже есть сведения; иначе сторож спрашивает прямыми запросами, как
    раньше. `covered` — префиксы учётных данных (`auth.env_prefix` манифеста) источников, чей доступ задан режимом: такие
    серверы сторож отдельно не опрашивает, их состояние уже в сводке."""
    none = {"known": False, "ok": True, "down": [], "covered": []}
    try:
        svc = svc or CN.service()
        if not svc.enabled:
            return none
        need = required_for(project, svc)
        svc.ensure(profiles=need["profiles"], budget=budget)
        snap = svc.snapshot(need)
        if snap.get("state") == "unknown":
            return none
        down = []
        if need["internet"] and (snap.get("internet") or {}).get("state") == "offline":
            down.append("интернет")
        down += [r["name"] for r in snap.get("profiles") or [] if r.get("required") and r["state"] == "offline"]
        down += [f"модель ({r['name'] or r['id']})" for r in snap.get("providers") or []
                 if r.get("required") and r["state"] == "offline"]
        covered = sorted({s["prefix"] for s in source_instances(project) if s["prefix"]
                          and (needs_of_source(s)["profile"] or needs_of_source(s)["internet"])}) if project else []
        return {"known": True, "ok": not down, "down": down, "covered": covered}
    except Exception:  # noqa: BLE001 — сторож не падает из-за проверки связи
        return none


_NOTED: dict = {}
NOTE_COOLDOWN = 5.0     # секунд: десяток потоков, оборвавшихся разом, перепроверяют связь один раз


def note_failure(provider: str, svc=None) -> dict:
    """Вызов провайдера оборвался по связи: сразу перепроверить его профиль и интернет (на месте, в пределах бюджета),
    а не ждать следующего хода фоновой проверки → {provider_id: причина} недоступных на сейчас."""
    import time
    svc = svc or CN.service()
    try:
        if not svc.enabled:
            return {}
        if time.monotonic() - _NOTED.get(provider, -1e9) < NOTE_COOLDOWN:
            return providers_down(svc)
        _NOTED[provider] = time.monotonic()
        row = next((p for p in svc.providers() if p["id"] == provider), None)
        only = {CN.INTERNET}
        if row and row.get("profile"):
            only.add(row["profile"])
        if row and row.get("check") != "off":
            only.add(CN.provider_profile(provider))
        svc.check(only=sorted(only), budget=CN.PRELAUNCH_BUDGET)
    except Exception:  # noqa: BLE001
        return {}
    return providers_down(svc, budget=0.5, fresh=True)


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
        variant = row.get("offline_variant", "")
        if variant not in ("",) + KNOWN_VARIANTS:
            problems.append(f"{DEPS_NAME}: {cmd}: вариант без связи «{str(variant)[:30]}» неизвестен "
                            f"({', '.join(KNOWN_VARIANTS)}) — у закладки «Команды» для него нет подписи")
        if row.get("offline_capable") is True and (row.get("requires_llm") is True or row.get("requires_internet") is True
                                                   or row.get("requires_sources")):
            problems.append(f"{DEPS_NAME}: {cmd}: «offline_capable» противоречит «requires_*» — команде нужна связь, "
                            "а запись обещает, что без неё она работает")
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
