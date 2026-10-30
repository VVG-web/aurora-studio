#!/usr/bin/env python3
"""connector_ops.py — заменяемые коннекторы: операции трекера задач и вики по роли (фреймворк «Аврора»).

Ядро не знает, какой трекер задач или какая вики стоит у проекта. Оно знает роли (`tasks` — трекер задач,
`wiki` — вики) и имена операций; коннектор объявляет в манифесте (`connector.json`, поле `operations`), какую роль
он играет, какие операции умеет и каким скриптом их выполняет:

    "operations": {"role": "tasks", "script": "<скрипт>.py", "ops": ["issue_types", "epics", …],
                   "server": "base_url", "defaults": {"project_key": "project_key"}}

Сменить трекер — значит поставить другой коннектор с той же ролью и выбрать его в «Настройках проекта»: код Авроры
при этом не меняется. Активный коннектор роли — блок `connectors:` конфига проекта:

    connectors:
      tasks: <id коннектора>
      wiki: <id коннектора>

Блока или строки роли нет — работает тот единственный установленный коннектор, что объявляет роль (`operations.role`;
копия манифеста старше поля — по роли сервиса: `tasktracker` для `tasks`, `projectwiki` для `wiki`). Объявляют
несколько — выбор за человеком, без него операция не идёт.

Операция — отдельный процесс: скрипт операций коннектора из копии движка проекта (`.aurora/scripts/`) получает на
stdin `{"op", "params", "project"}` и пишет в stdout `{"ok": true, "result": …}` либо `{"ok": false, "kind": …,
"error": …}`. Вид отказа (`kind`) ядро называет человеку по таблице `CAUSES` (ru и en), текст коннектора едет рядом
(`detail`). `server` — какая настройка коннектора (в его блоке `settings_block`) называет сервер: она входит в ключ
кэша; `defaults` — какие параметры операций берутся из его настроек по умолчанию (ключ проекта трекера).

Списки для форм (типы задач, эпики) кэшируются на час в `.aurora/state/cache/connectors/` проекта: ключ — коннектор,
адрес сервера, операция и параметры; ключей доступа нет ни в имени, ни в файле. `fresh` — взять заново.

  python3 .aurora/scripts/connector_ops.py                       # какие коннекторы активны для ролей
  python3 .aurora/scripts/connector_ops.py --role tasks --op issue_types --params '{"project_key": "PRJA"}'

Контракт ролей и операций — `docs/connectors.md`. Модуль — библиотека ядра: его зовут `ship:task` и сервер панели.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на «—» (UnicodeEncodeError); движок пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import ext_safe_name, top_block_span, yaml_scalar  # noqa: E402
import sources_registry as SR  # noqa: E402

CONFIG = "aurora.config.yaml"
BLOCK = "connectors"
# Роли операций и их стандартный набор (контракт роли). Коннектор может уметь не всё: чего нет в его `ops`, на то
# ядро отвечает «не умеет», а не пробует наугад.
ROLES = {
    "tasks": ("issue_types", "epics", "find_user", "create_issue", "link_page", "get_issue"),
    "wiki": ("site", "find_page", "get_page", "children", "create_page", "update_page", "move_page", "page_space"),
}
# Роль сервиса (`service_role` манифеста), по которой находится коннектор без поля `operations` (копия манифеста
# старше поля): он в выборе есть, а операция отвечает «обновите движок проекта».
SERVICE_OF = {"tasks": "tasktracker", "wiki": "projectwiki"}
CACHED = {("tasks", "issue_types"), ("tasks", "epics")}
CACHE_TTL = 3600
CACHE_DIR = os.path.join(".aurora", "state", "cache", "connectors")
OP_TIMEOUT = 300
_OP = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ID = re.compile(r"^[A-Za-z0-9][\w.\-]{0,63}$")
clock = time.time            # часы кэша: проверка подменяет их, а не ждёт час
_LOCK = threading.Lock()

# Причины отказа: вид → текст на ru и en. Подстановки — `{id}` коннектора, `{op}`, `{role}`, `{ids}`, `{roles}`.
# Панель переводит их по этой таблице (`aurora_cockpit._cause_messages`), `kit:i18n --check` сверяет полноту.
CAUSES: dict = {
    "bad_role": {
        "ru": "роль «{role}» движку неизвестна (есть: {roles})",
        "en": "the role «{role}» is unknown to the engine (known: {roles})"},
    "no_connector": {
        "ru": "ни один установленный коннектор не объявляет роль «{role}» — поставьте коннектор с этой ролью",
        "en": "no installed connector declares the role «{role}» — install a connector with this role"},
    "ambiguous": {
        "ru": "роль «{role}» объявляют несколько коннекторов ({ids}) — выберите один в «Настройках проекта»",
        "en": "several connectors declare the role «{role}» ({ids}) — choose one in Project settings"},
    "unknown_connector": {
        "ru": "для роли «{role}» выбран коннектор «{id}», но он не установлен или эту роль не объявляет — "
              "выберите другой в «Настройках проекта»",
        "en": "the connector «{id}» is chosen for the role «{role}», but it is not installed or does not declare "
              "this role — choose another one in Project settings"},
    "unsupported": {
        "ru": "коннектор «{id}» не умеет операцию «{op}» роли «{role}» — обновите движок проекта («Версия» → "
              "«Подготовить проект») или выберите другой коннектор",
        "en": "the connector «{id}» cannot do the operation «{op}» of the role «{role}» — update the project engine "
              "(Version → Prepare project) or choose another connector"},
    "no_script": {
        "ru": "у коннектора «{id}» не найден скрипт операций — движок проекта неполный: «Версия» → «Подготовить проект»",
        "en": "the operations script of the connector «{id}» is not found — the project engine is incomplete: "
              "Version → Prepare project"},
    "config": {
        "ru": "коннектор «{id}» не настроен: нет адреса сервера или другой обязательной настройки в «Настройках проекта»",
        "en": "the connector «{id}» is not set up: the server address or another required setting is missing in "
              "Project settings"},
    "auth": {
        "ru": "коннектор «{id}»: входа нет или сервер его не принял — проверьте ключ в .env.aurora.local",
        "en": "the connector «{id}»: no sign-in or the server rejected it — check the key in .env.aurora.local"},
    "forbidden": {
        "ru": "коннектор «{id}»: у ключа нет прав на операцию «{op}»",
        "en": "the connector «{id}»: the key has no rights for the operation «{op}»"},
    "not_found": {
        "ru": "коннектор «{id}»: сервер не нашёл то, о чём спросила операция «{op}»",
        "en": "the connector «{id}»: the server did not find what the operation «{op}» asked for"},
    "bad_request": {
        "ru": "коннектор «{id}»: сервер отклонил запрос операции «{op}» — причина в подробностях",
        "en": "the connector «{id}»: the server rejected the request of the operation «{op}» — see the details"},
    "conflict": {
        "ru": "коннектор «{id}»: на сервере уже другая версия — перечитайте и повторите",
        "en": "the connector «{id}»: the server already has another version — reload and try again"},
    "net": {
        "ru": "коннектор «{id}»: сервер не ответил — проверьте сеть или VPN",
        "en": "the connector «{id}»: the server did not answer — check the network or the VPN"},
    "unsafe": {
        "ru": "коннектор «{id}»: ключ на этот адрес не отправляется — адрес сервера сменился или это открытый http",
        "en": "the connector «{id}»: the key is not sent to this address — the server address changed or it is "
              "plain http"},
    "bad_response": {
        "ru": "коннектор «{id}» ответил не по контракту операций: нет строки JSON с полем ok",
        "en": "the connector «{id}» did not answer by the operations contract: no JSON line with the ok field"},
    "failed": {
        "ru": "коннектор «{id}»: операция «{op}» не выполнена",
        "en": "the connector «{id}»: the operation «{op}» failed"},
    "save_conflict": {
        "ru": "коннекторы ролей изменились на диске с момента открытия страницы — откройте настройки заново",
        "en": "the role connectors changed on disk since the page was opened — open the settings again"},
    "no_config": {
        "ru": "в проекте нет aurora.config.yaml",
        "en": "the project has no aurora.config.yaml"},
    "not_utf8": {
        "ru": "aurora.config.yaml не в кодировке UTF-8 — файл не тронут",
        "en": "aurora.config.yaml is not in UTF-8 — the file is untouched"},
    "write": {
        "ru": "не удалось записать aurora.config.yaml",
        "en": "could not write aurora.config.yaml"},
}


class _Args(dict):
    def __missing__(self, key):
        return "?"


def why(kind: str, lang: str = "ru", **args) -> str:
    """Причина отказа словами на языке `lang` (нет перевода — по-русски); неизвестный вид — `failed`."""
    row = CAUSES.get(kind) or CAUSES["failed"]
    return (row.get(lang) or row["ru"]).format_map(_Args({k: str(v) for k, v in args.items()}))


def failure(kind: str, **args) -> dict:
    return {"ok": False, "kind": kind, "error": why(kind, **args)}


# ------------------------------------------------------------------ кто играет роль

def ops_of(m: dict) -> list:
    """Операции, которые объявил коннектор."""
    ops = (m.get("operations") or {}).get("ops")
    return [o for o in ops if isinstance(o, str)] if isinstance(ops, list) else []


def declares(m: dict, role: str) -> bool:
    """Играет ли коннектор роль: по `operations.role`, а у манифеста без операций — по роли сервиса."""
    ops = m.get("operations")
    if isinstance(ops, dict) and ops.get("role"):
        return ops["role"] == role
    return bool(m.get("service_role")) and m.get("service_role") == SERVICE_OF.get(role)


def candidates(project: str, role: str) -> list:
    """Установленные у проекта коннекторы, которые объявляют роль, — по id."""
    return [m for _mid, m in sorted(SR.installed(project or ".").items()) if declares(m, role)]


def _config(project: str) -> tuple:
    """(текст конфига, код отказа): не прочитан — None и `no_config`, `not_utf8` или `write`."""
    try:
        with open(os.path.join(project or ".", CONFIG), encoding="utf-8", newline="") as f:
            return f.read(), ""
    except FileNotFoundError:
        return None, "no_config"
    except UnicodeDecodeError:
        return None, "not_utf8"
    except OSError:
        return None, "write"


def chosen_of(text: str) -> dict:
    """{роль: id} из блока `connectors:` конфига; блока нет — пусто."""
    start, end = top_block_span(text or "", BLOCK)
    out = {}
    if start < 0:
        return out
    for line in text[start:end].splitlines()[1:]:
        m = re.match(r"""^[ \t]+([a-z][a-z0-9_]*)[ \t]*:[ \t]*["']?([A-Za-z0-9][\w.\-]*)?["']?[ \t]*(?:#.*)?$""",
                     line.rstrip("\r"))
        if m and m.group(2):
            out[m.group(1)] = m.group(2)
    return out


def pick(project: str, role: str) -> tuple:
    """(манифест активного коннектора роли | None, отказ | None, откуда: `config` | `single` | "")."""
    if role not in ROLES:
        return None, failure("bad_role", role=role, roles=", ".join(ROLES)), ""
    want = chosen_of(_config(project)[0] or "").get(role, "")
    cands = candidates(project, role)
    if want:
        m = next((c for c in cands if c["id"] == want), None)
        if m is None:
            return None, failure("unknown_connector", role=role, id=want), ""
        return m, None, "config"
    if len(cands) == 1:
        return cands[0], None, "single"
    if not cands:
        return None, failure("no_connector", role=role), ""
    return None, failure("ambiguous", role=role, ids=", ".join(c["id"] for c in cands)), ""


def active(project: str, role: str) -> str:
    """id коннектора, который у проекта сейчас играет роль; пусто — никто (или выбор не сделан)."""
    m, _fail, _src = pick(project, role)
    return m["id"] if m else ""


def supports(project: str, role: str, op: str) -> bool:
    """Умеет ли активный коннектор роли эту операцию."""
    m, _fail, _src = pick(project, role)
    return bool(m) and op in ops_of(m)


# ------------------------------------------------------------------ настройки коннектора

def block_body(text: str, dotted: str) -> str:
    """Тело вложенного раздела конфига `a.b` (строки глубже его ключа); раздела нет — пусто."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    lo, hi, indent = 0, len(lines), -1
    for part in [p for p in str(dotted or "").split(".") if p]:
        at = None
        for i in range(lo, hi):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            ind = len(line) - len(line.lstrip(" \t"))
            if ind > indent and re.match(r"[ \t]*%s[ \t]*:[ \t]*(?:#.*)?$" % re.escape(part), line):
                at, key_ind = i, ind
                break
        if at is None:
            return ""
        end = at + 1
        while end < hi and not (lines[end].strip() and not lines[end].lstrip().startswith("#")
                                and len(lines[end]) - len(lines[end].lstrip(" \t")) <= key_ind):
            end += 1
        lo, hi, indent = at + 1, end, key_ind
    return "\n".join(lines[lo:hi]) if indent >= 0 else ""


def setting(project: str, m: dict, key: str) -> str:
    """Скалярная настройка коннектора из его блока конфига (`settings_block` манифеста); нет — пусто."""
    block = m.get("settings_block") or ""
    if not (block and key):
        return ""
    return yaml_scalar(block_body(_config(project)[0] or "", block), key).rstrip("/")


def defaults(project: str, role: str) -> dict:
    """Параметры операций по умолчанию из настроек активного коннектора (`operations.defaults` манифеста)."""
    m, _fail, _src = pick(project, role)
    out = {}
    for param, key in (((m or {}).get("operations") or {}).get("defaults") or {}).items():
        value = setting(project, m, str(key)) if isinstance(key, str) else ""
        if value:
            out[str(param)] = value
    return out


# ------------------------------------------------------------------ вызов

def script_of(project: str, m: dict) -> str:
    """Путь скрипта операций: копия движка проекта, иначе папка манифеста, иначе кит (папка коннектора, `scripts/`)."""
    name = (m.get("operations") or {}).get("script") or ""
    if not ext_safe_name(name):
        return ""
    here = os.path.dirname(os.path.abspath(__file__))
    places = [os.path.join(project or ".", ".aurora", "scripts", name)]
    if m.get("_path"):
        places.append(os.path.join(os.path.dirname(m["_path"]), name))
    for kit in dict.fromkeys(k for k in (SR.real_kit_root(), os.path.dirname(here)) if k):
        places += [os.path.join(kit, "connectors", m["id"], name), os.path.join(kit, "scripts", name)]
    return next((os.path.abspath(p) for p in places if os.path.isfile(p)), "")


def _run(project: str, script: str, op: str, params: dict) -> dict:
    """Один вызов скрипта операций → его ответ (`ok`, `result` | `kind`, `error`)."""
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    req = json.dumps({"op": op, "params": params, "project": os.path.abspath(project or ".")})
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
    try:
        cp = subprocess.run([sys.executable, script], input=req, cwd=project or ".", env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=OP_TIMEOUT, **extra)
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "net", "error": f"операция «{op}» не ответила за {OP_TIMEOUT} с"}
    except OSError as e:
        return {"ok": False, "kind": "failed", "error": f"{type(e).__name__}: {e}"}
    for line in reversed((cp.stdout or "").strip().splitlines()):
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict) and isinstance(got.get("ok"), bool):
            return got
    tail = ((cp.stderr or "").strip().splitlines() or [""])[-1]
    return {"ok": False, "kind": "bad_response", "error": f"rc={cp.returncode} {tail}".strip()[:500]}


def _cache_file(project: str, key: str) -> str:
    return os.path.join(project or ".", CACHE_DIR, key + ".json")


def cache_get(project: str, key: str):
    """Запись кэша, если она моложе часа по часам `clock`; иначе None."""
    try:
        with open(_cache_file(project, key), encoding="utf-8") as f:
            hit = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(hit, dict) or not isinstance(hit.get("at"), (int, float)):
        return None
    return hit if 0 <= clock() - hit["at"] < CACHE_TTL else None


def cache_put(project: str, key: str, entry: dict) -> None:
    """Записать ответ в кэш проекта (вне git). Не записалось — не беда: в следующий раз спросим сервер."""
    from aurora_fs import write_atomic
    try:
        write_atomic(_cache_file(project, key), json.dumps(entry, ensure_ascii=False), fsync=False)
    except (OSError, ValueError):
        pass


def call(project: str, role: str, op: str, params: dict | None = None, fresh: bool = False) -> dict:
    """Выполнить операцию роли у активного коннектора → `{ok: true, result, connector, cached}` либо
    `{ok: false, kind, error, detail, connector[, status]}`. `error` — причина словами (`CAUSES`), `detail` — текст
    коннектора, `status` — код ответа сервера, если отказал сервер (обрыв связи кода не имеет)."""
    project = project or "."
    params = dict(params) if isinstance(params, dict) else {}
    m, fail, _src = pick(project, role)
    if fail:
        return fail
    cid = m["id"]
    if not (_OP.match(str(op or "")) and op in ops_of(m)):
        return dict(failure("unsupported", id=cid, op=op, role=role), connector=cid)
    script = script_of(project, m)
    if not script:
        return dict(failure("no_script", id=cid), connector=cid)
    key = server = ""
    if (role, op) in CACHED:
        server = setting(project, m, str((m.get("operations") or {}).get("server") or ""))
        blob = json.dumps([cid, server, op, params], ensure_ascii=False, sort_keys=True)
        key = f"{role}-{op}-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
        hit = None if fresh else cache_get(project, key)
        if hit is not None:
            return {"ok": True, "result": hit.get("result"), "connector": cid, "cached": True, "at": hit["at"]}
    got = _run(project, script, op, params)
    if not got.get("ok"):
        kind = got.get("kind") if got.get("kind") in CAUSES else "failed"
        out = {"ok": False, "kind": kind, "error": why(kind, id=cid, op=op, role=role),
               "detail": str(got.get("error") or "")[:500], "connector": cid}
        if isinstance(got.get("status"), int) and not isinstance(got.get("status"), bool):
            out["status"] = got["status"]           # код ответа сервера: отказ сервера, а не обрыв связи
        return out
    out = {"ok": True, "result": got.get("result"), "connector": cid, "cached": False, "at": clock()}
    if key:
        cache_put(project, key, {"at": out["at"], "connector": cid, "server": server, "op": op, "params": params,
                                 "result": out["result"]})
    return out


# ------------------------------------------------------------------ настройка проекта: блок `connectors:`

def digest(text: str) -> str:
    """Слепок блока `connectors:` — страница шлёт его при записи (`expect`); меняется, только если менялся блок."""
    start, end = top_block_span(text or "", BLOCK)
    body = text[start:end] if start >= 0 else ""
    return hashlib.sha1(body.replace("\r\n", "\n").encode("utf-8")).hexdigest()[:16]


def render(chosen: dict) -> str:
    """Текст блока `connectors:`; ролей нет — пусто (блок уходит, работает выбор по умолчанию)."""
    rows = [f"  {role}: {cid}\n" for role, cid in chosen.items() if cid]
    return ("connectors:   # активный коннектор роли: трекер задач (tasks), вики (wiki) — «Настройки проекта»\n"
            + "".join(rows)) if rows else ""


def replace(text: str, chosen: dict) -> str:
    """Конфиг с блоком `connectors:` по `chosen`; остальной текст — байт в байт, окончания строк — как в файле."""
    text = text or ""
    nl = "\r\n" if "\r\n" in text else "\n"
    new = render(chosen).replace("\n", nl)
    start, end = top_block_span(text, BLOCK)
    if start >= 0:
        before, after = text[:start], text[end:]
        if not new:                       # блок уходит вместе с отбивкой, которую ему ставила запись
            if not after.strip():
                return before.rstrip("\r\n") + nl if before.strip() else ""
            if before.endswith(nl + nl) and after.startswith(nl):
                after = after[len(nl):]
        return before + new + after
    if not new:
        return text
    base = text
    if base and not base.endswith("\n"):
        base += nl
    if base and not base.endswith(nl + nl):
        base += nl
    return base + new


def state(project: str) -> dict:
    """Роли проекта для «Настроек проекта»: кто активен и почему, из кого выбирать, чего не умеет, слепок блока."""
    text, problem = _config(project)
    chosen = chosen_of(text or "")
    roles = {}
    for role, standard in ROLES.items():
        m, fail, src = pick(project, role)
        row = {"active": m["id"] if m else "", "from": src, "chosen": chosen.get(role, ""),
               "choices": [{"id": c["id"], "title": c.get("title") or c["id"], "ops": ops_of(c),
                            "missing": [o for o in standard if o not in ops_of(c)]}
                           for c in candidates(project, role)]}
        if fail:
            row.update(kind=fail["kind"], error=fail["error"])
        roles[role] = row
    return {"roles": roles, "digest": digest(text or ""), "problem": problem,
            **({"error": why(problem)} if problem else {})}


def save(project: str, roles, expect) -> dict:
    """Записать блок `connectors:` → {ok, digest, chosen} либо {ok: False, code, error[, conflict | failed]}.

    `roles` — {роль: id}; пустой id — вернуть роли выбор по умолчанию. Меняется только блок `connectors:`, остальной
    конфиг — байт в байт. `expect` — слепок блока, с которым страница его открыла: другой (или не прислан) — отказ
    `conflict`. Запись неделимая; конфиг не в UTF-8 не трогается."""
    if not isinstance(roles, dict):
        return {"ok": False, "code": "bad_role", "error": why("bad_role", role="?", roles=", ".join(ROLES))}
    clean = {}
    for role, cid in roles.items():
        cid = str(cid or "").strip()
        if role not in ROLES:
            return {"ok": False, "code": "bad_role", "error": why("bad_role", role=role, roles=", ".join(ROLES))}
        if cid and (not _ID.match(cid) or cid not in [c["id"] for c in candidates(project, role)]):
            return {"ok": False, "code": "unknown_connector", "error": why("unknown_connector", role=role, id=cid)}
        clean[role] = cid
    from aurora_fs import write_atomic
    with _LOCK:
        old, problem = _config(project)
        if problem:
            return {"ok": False, "code": problem, "error": why(problem), "failed": problem == "write"}
        if not isinstance(expect, str) or expect != digest(old):
            return {"ok": False, "code": "conflict", "conflict": True, "error": why("save_conflict")}
        chosen = chosen_of(old)
        chosen.update(clean)
        chosen = {r: c for r, c in chosen.items() if c}
        new = replace(old, chosen)
        if new != old:
            try:
                write_atomic(os.path.join(project, CONFIG), new, newline="", fsync=False)
            except (OSError, ValueError):
                return {"ok": False, "code": "write", "failed": True, "error": why("write")}
    return {"ok": True, "digest": digest(new), "chosen": chosen}


# ------------------------------------------------------------------ консоль

def main() -> int:
    ap = argparse.ArgumentParser(description="Коннекторы ролей: трекер задач и вики")
    ap.add_argument("--role", help="роль: " + ", ".join(ROLES))
    ap.add_argument("--op", help="операция роли; без неё — какие коннекторы активны")
    ap.add_argument("--params", default="{}", help="параметры операции объектом JSON")
    ap.add_argument("--fresh", action="store_true", help="списки — заново, мимо кэша")
    a = ap.parse_args()
    if a.op:
        try:
            params = json.loads(a.params)
        except ValueError as e:
            print(f"connector_ops: --params — не JSON: {e}", file=sys.stderr)
            return 2
        res = call(project=".", role=a.role or "", op=a.op, params=params, fresh=a.fresh)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if res.get("ok") else 1
    st = state(".")
    for role, row in st["roles"].items():
        if row["active"]:
            how = "выбран в настройках проекта" if row["from"] == "config" else "единственный с этой ролью"
            print(f"{role}: {row['active']} ({how}); умеет: {', '.join(next((c['ops'] for c in row['choices'] if c['id'] == row['active']), [])) or '—'}")
        else:
            print(f"{role}: — {row.get('error', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
