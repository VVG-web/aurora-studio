#!/usr/bin/env python3
"""GitLab-группы Code: discovery репозиториев и подсветка новых.

Блок `code_groups:` в `aurora.config.yaml` — часть проекта, как `code_repos:`. Он не
хранит токены: вход берётся из того же `git_sync`-хранилища секретов, что и у
репозиториев. Группа задаёт URL GitLab, признак подгрупп и два списка путей:
`selected` — выбранные репозитории, `known` — уже показанные/выбранные. Новый
репозиторий — путь, которого нет в `known`; после выбора он попадает в `known`.
Через 14 дней с последнего открытия выбора `known` сбрасывается в текущий список,
чтобы старая подсветка не жила вечно.

Только API и модель; клонирование делает существующий `code:sync`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import code_repos as CREPO
import git_sync as GS

CONFIG = "aurora.config.yaml"
KEY = "code_groups"
FIELDS = ("url", "include_subgroups", "last_seen_at", "selected", "known", "repo_names")
LIST_FIELDS = ("selected", "known", "repo_names")
NEW_DAYS = 14
PAGE_LIMIT = 20
API_TIMEOUT = 20
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEAD_RE = re.compile(r"^code_groups[ \t]*:[ \t]*(?:#.*)?$")
_LOCK = threading.Lock()


def config_path(project) -> Path:
    return Path(project) / CONFIG


def _yaml_str(value) -> str:
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _block_span(lines: list) -> tuple | None:
    start = next((i for i, ln in enumerate(lines) if _HEAD_RE.match(ln.rstrip("\r"))), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
        end += 1
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def blank() -> dict:
    return {"url": "", "include_subgroups": True, "last_seen_at": "",
            "selected": [], "known": [], "repo_names": []}


def _bool(value, default=True) -> bool:
    if isinstance(value, bool):
        return value
    low = str(value if value is not None else "").strip().strip("\"'").lower()
    if low in ("true", "yes", "on", "1", "да"):
        return True
    if low in ("false", "no", "off", "0", "нет", ""):
        return False
    return default


def parse(text: str) -> dict:
    """{имя: группа} из текста конфига; списки читаются и как YAML-список, и из одной строки."""
    text = text[1:] if text.startswith("\ufeff") else text
    lines = text.split("\n")
    span = _block_span(lines)
    if not span:
        return {}
    groups: dict = {}
    cur = None
    i = span[0] + 1
    while i < span[1]:
        line = lines[i].rstrip("\r")
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"\s*([^\s:#][^:#]*?)\s*:(?:\s+(.*))?$", line)
        if not m:
            continue
        key, value = CREPO._unquote(m.group(1)), m.group(2) or ""
        if indent <= 2 and not value.strip():
            cur = key
            groups[cur] = blank()
            continue
        if cur is None or indent < 4:
            continue
        if key in LIST_FIELDS:
            items = []
            # Инлайн-список: selected: ["a", "b"]
            raw = value.strip()
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                    items = [str(x) for x in parsed] if isinstance(parsed, list) else []
                except ValueError:
                    items = [x.strip().strip("\"'") for x in raw.strip("[]").split(",") if x.strip()]
            else:
                if raw:
                    items = [x.strip().strip("\"'") for x in raw.split(",") if x.strip()]
                while i < span[1]:
                    nxt = lines[i].rstrip("\r")
                    mm = re.match(r"^\s*-\s+(.*)$", nxt)
                    if not mm or len(nxt) - len(nxt.lstrip()) < 6:
                        break
                    items.append(CREPO._unquote(mm.group(1)))
                    i += 1
            groups[cur][key] = items
        elif key in ("url", "last_seen_at"):
            groups[cur][key] = CREPO._unquote(value)
        elif key == "include_subgroups":
            groups[cur][key] = _bool(value)
    return groups


def read_groups(project=".") -> dict:
    try:
        text = config_path(project).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return parse(text)


def digest(groups: dict) -> str:
    norm = {n: {f: (sorted(rec.get(f) or []) if f in LIST_FIELDS else
                    (bool(rec.get(f)) if f == "include_subgroups" else str(rec.get(f) or "")))
                for f in FIELDS} for n, rec in (groups or {}).items()}
    return hashlib.sha256(json.dumps(norm, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def name_problem(name: str) -> str:
    if not str(name or "").strip():
        return "name_empty"
    if not NAME_RX.match(str(name)):
        return "name_chars"
    return ""


def normalize(name: str, rec) -> tuple:
    rec = rec if isinstance(rec, dict) else {}
    out, probs = blank(), []
    out["url"] = GS.strip_userinfo(str(rec.get("url") or "").strip()).rstrip("/")
    out["include_subgroups"] = _bool(rec.get("include_subgroups"), True)
    out["last_seen_at"] = str(rec.get("last_seen_at") or "").strip()
    for field in LIST_FIELDS:
        raw = rec.get(field)
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        vals, seen = [], set()
        for item in raw if isinstance(raw, (list, tuple)) else []:
            item = str(item or "").strip().strip("/")
            if not item or item in seen:
                continue
            if "\n" in item or "\r" in item or ".." in item.split("/"):
                probs.append({"name": name, "field": field, "code": "bad_path"})
                continue
            seen.add(item)
            vals.append(item)
        out[field] = vals
    why = name_problem(name)
    if why:
        probs.append({"name": name, "field": "name", "code": why})
    if not out["url"]:
        probs.append({"name": name, "field": "url", "code": "required"})
    elif not GS.is_url(out["url"]) or not out["url"].lower().startswith(("http://", "https://")):
        probs.append({"name": name, "field": "url", "code": "bad_url"})
    if out["last_seen_at"]:
        try:
            datetime.fromisoformat(out["last_seen_at"].replace("Z", "+00:00"))
        except ValueError:
            probs.append({"name": name, "field": "last_seen_at", "code": "bad_date"})
    return out, probs


def check(groups: dict) -> tuple:
    out, probs, seen = {}, [], set()
    for name, rec in (groups or {}).items():
        name = str(name)
        if name.casefold() in seen:
            probs.append({"name": name, "field": "name", "code": "name_duplicate"})
        seen.add(name.casefold())
        row, p = normalize(name, rec)
        out[name] = row
        probs += p
    return out, probs


def block_lines(groups: dict) -> list:
    lines = [KEY + ":"]
    for name in sorted(groups, key=str.casefold):
        rec = groups[name]
        lines.append(f"  {name}:")
        lines.append(f"    url: {_yaml_str(rec['url'])}")
        lines.append(f"    include_subgroups: {'true' if rec['include_subgroups'] else 'false'}")
        if rec.get("last_seen_at"):
            lines.append(f"    last_seen_at: {_yaml_str(rec['last_seen_at'])}")
        for field in LIST_FIELDS:
            if not rec.get(field):
                continue
            lines.append(f"    {field}:")
            for item in sorted(rec[field], key=str.casefold):
                lines.append(f"      - {_yaml_str(item)}")
    return lines


def write_block(config_text: str, groups: dict) -> str:
    bom = "\ufeff" if config_text.startswith("\ufeff") else ""
    lines = config_text[len(bom):].split("\n")
    span = _block_span(lines)
    block = block_lines(groups) if groups else []
    if span is None:
        if not block:
            return config_text
        last = next((ln for ln in reversed(lines) if ln.strip()), "")
        cr = "\r" if last.endswith("\r") else ""
        head = "\n".join(lines).rstrip("\r\n")
        return bom + (head + (cr + "\n") * 2 if head else "") + "\n".join(ln + cr for ln in block) + "\n"
    start, end = span
    cr = "\r" if lines[start].endswith("\r") else ""
    rest = lines[end:]
    if not block:
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        while start == 0 and len(rest) > 1 and not rest[0].strip():
            rest = rest[1:]
        return bom + "\n".join(lines[:start] + rest)
    return bom + "\n".join(lines[:start] + [ln + cr for ln in block] + rest)


def save(project, groups, expect: str = "") -> dict:
    """Записать блок групп; `expect` — слепок, который видела форма. Соседние секции не меняются."""
    if not isinstance(groups, dict):
        return {"error": "bad_groups"}
    if not isinstance(expect, str):
        return {"error": "bad_expect"}
    path = config_path(project)
    if not path.is_file():
        return {"error": "no_config"}
    clean, probs = check(groups)
    if probs:
        return {"error": "problems", "problems": probs}
    with _LOCK:
        try:
            old_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {"error": "config_unreadable", "failed": True}
        if expect and expect != digest(parse(old_text)):
            return {"conflict": True, "error": "conflict", **state(project)}
        new_text = write_block(old_text, clean)
        if new_text != old_text:
            try:
                import aurora_fs
                aurora_fs.write_atomic(str(path), new_text, newline="", makedirs=False)
            except OSError:
                return {"error": "config_unwritable", "failed": True}
    return {"ok": True, "digest": digest(parse(new_text)), "groups": len(clean)}


def state(project) -> dict:
    groups = read_groups(project)
    clean, probs = check(groups)
    return {"groups": [{"name": n, **rec} for n, rec in clean.items()],
            "digest": digest(groups), "problems": probs}


# ---------------------------------------------------------------- GitLab API

def _http():
    """Отдельная точка для тестов: подменяется, не трогая реальную сеть."""
    return GS.make_http(None)


def _api_base(url: str) -> tuple:
    p = urllib.parse.urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    path = p.path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return origin + "/api/v4", path


def cred_remote(name: str, url: str) -> str:
    """Ключ секретов группы привязан к хосту: смена URL не уносит старый токен на чужой сервер."""
    return f"group:{name}@{GS.hostport(url or '') or 'unknown'}"


def _auth_headers(project, name: str, credentials=None, url: str = "") -> tuple:
    """(заголовки, вход) из формы или из хранилища. Секрет наружу не отдаётся."""
    c = credentials if isinstance(credentials, dict) else None
    if not c:
        c = GS.cred_load(project, cred_remote(name, url))
    if not c or not str(c.get("secret") or "").strip() or c.get("secret") == GS.MASK:
        # Группа часто открывается до первого репозитория: берём вход уже настроенного
        # репозитория на том же хосте. Это существующий механизм, без нового поля в конфиге.
        host = GS.hostport(url or read_groups(project).get(name, {}).get("url") or "")
        for repo_name, rec in CREPO.read_repos(project).items():
            if host and GS.hostport(rec.get("url") or "") == host:
                c = CREPO.creds_of(project, repo_name)
                if c.get("secret"):
                    break
    secret = str((c or {}).get("secret") or "").strip()
    if not secret or secret == GS.MASK:
        return {}, {}
    auth = str((c or {}).get("auth") or "token")
    user = str((c or {}).get("user") or "")
    if auth in ("password", "ldap"):
        if not user:
            return {}, c
        token = base64.b64encode(f"{user}:{secret}".encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}, c
    return {"PRIVATE-TOKEN": secret}, c


def _iso_days_old(value: str) -> bool:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt > timedelta(days=NEW_DAYS)
    except (TypeError, ValueError):
        return False


def _project_row(p: dict) -> dict:
    path = str(p.get("path_with_namespace") or "").strip("/")
    return {
        "id": p.get("id"),
        "path": path,
        "name": p.get("name") or path.rsplit("/", 1)[-1],
        "url": p.get("http_url_to_repo") or p.get("ssh_url_to_repo") or "",
        "default_branch": p.get("default_branch") or "",
        "created_at": p.get("created_at") or "",
        "last_activity_at": p.get("last_activity_at") or "",
        "web_url": p.get("web_url") or "",
    }


def fetch_projects(project, name: str, group=None, credentials=None, now=None) -> dict:
    """Список проектов группы GitLab с пагинацией и `include_subgroups`; `new` — относительно known.

    Возвращает `group` с обновлённым `last_seen_at`/`known`; сохранение делает вызывающий.
    """
    group = group if isinstance(group, dict) else read_groups(project).get(name) or blank()
    rec, probs = normalize(name, group)
    if probs:
        return {"ok": False, "error": "problems", "problems": probs}
    if not rec["url"]:
        return {"ok": False, "error": "url_required"}
    base, group_path = _api_base(rec["url"])
    if not group_path:
        return {"ok": False, "error": "group_path_required"}
    headers, used_cred = _auth_headers(project, name, credentials, rec["url"])
    http = _http()
    rows, page = [], 1
    next_page = "1"
    while next_page and page <= PAGE_LIMIT:
        query = urllib.parse.urlencode({
            "include_subgroups": "true" if rec["include_subgroups"] else "false",
            "per_page": 100,
            "page": page,
            "order_by": "path",
            "sort": "asc",
        })
        url = f"{base}/groups/{urllib.parse.quote(group_path, safe='')}/projects?{query}"
        status, data, err, hdrs = http(url, headers, API_TIMEOUT)
        if status == 0:
            return {"ok": False, "error": "gitlab_unreachable", "detail": err[:300],
                    "hint": "проверьте URL и доступ к GitLab"}
        if status in (401, 403):
            return {"ok": False, "error": "gitlab_auth", "status": status,
                    "hint": "нужен вход GitLab с доступом к группе (Code → вход репозитория)"}
        if status == 404:
            return {"ok": False, "error": "gitlab_group_not_found", "status": 404}
        if status != 200 or not isinstance(data, list):
            return {"ok": False, "error": "gitlab_response", "status": status, "detail": err[:300]}
        rows += [_project_row(p) for p in data if isinstance(p, dict)]
        next_page = str((hdrs or {}).get("x-next-page") or "").strip()
        page += 1
        if not next_page:
            break
    if page > PAGE_LIMIT and next_page:
        return {"ok": False, "error": "gitlab_too_many_pages", "limit": PAGE_LIMIT}
    # Повтор одного пути (переименование/дубль в ответе) не должен раздувать выбор.
    uniq, seen = [], set()
    for row in rows:
        key = row["path"].casefold()
        if row["path"] and key not in seen:
            seen.add(key)
            uniq.append(row)
    current = [r["path"] for r in uniq]
    known = list(rec.get("known") or [])
    reset = bool(rec.get("last_seen_at") and _iso_days_old(rec["last_seen_at"]))
    if reset:
        known = current
    known_set = {x.casefold() for x in known}
    for row in uniq:
        row["new"] = row["path"].casefold() not in known_set
        row["repo_name"] = CREPO.suggest_name(row["url"]) or row["path"].rsplit("/", 1)[-1]
    out_group = {**rec, "known": known,
                 "last_seen_at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")}
    return {"ok": True, "projects": uniq, "group": out_group, "reset": reset,
            "auth": bool(used_cred), "new": sum(1 for r in uniq if r["new"])}


def discover(project, name: str, url: str, include_subgroups: bool = True, credentials=None) -> dict:
    """Discovery по URL; у существующей группы сохраняет last_seen_at (и сброс known)."""
    groups = read_groups(project)
    existing = groups.get(name)
    group = {**(existing or blank()), "url": str(url or "").rstrip("/"),
             "include_subgroups": bool(include_subgroups)}
    res = fetch_projects(project, name, group, credentials)
    if not res.get("ok"):
        return res
    res["saved"] = bool(existing)
    if existing:
        expect = digest(groups)
        groups[name] = res["group"]
        saved = save(project, groups, expect)
        if not saved.get("ok"):
            return {**saved, "stage": "groups"}
        res["digest"] = saved.get("digest", "")
    return res


def save_selection(project, name: str, url: str, selected, include_subgroups: bool = True,
                   repo_expect: str = "", credentials=None, projects=None) -> dict:
    """Сохранить выбранные проекты: добавить/убрать записи этой группы в `code_repos` и обновить `known`."""
    if not isinstance(selected, (list, tuple)):
        return {"error": "bad_selected"}
    selected = [x for x in selected if isinstance(x, (dict, str))]
    meta = {str(p.get("path") or p.get("path_with_namespace")): p for p in (projects or [])
            if isinstance(p, dict) and (p.get("path") or p.get("path_with_namespace"))}
    for item in selected:
        if isinstance(item, dict) and (item.get("path") or item.get("path_with_namespace")):
            meta.setdefault(str(item.get("path") or item.get("path_with_namespace")), item)
    groups = read_groups(project)
    old = groups.get(name) or blank()
    group = {**old, "url": str(url or old.get("url") or "").rstrip("/"),
             "include_subgroups": bool(include_subgroups)}
    paths = [str((x.get("path") or x.get("path_with_namespace")) if isinstance(x, dict) else x).strip("/")
             for x in selected]
    paths = [p for p in dict.fromkeys(paths) if p]
    _row, probs = normalize(name, {**group, "selected": paths, "known": old.get("known") or [],
                                   "repo_names": old.get("repo_names") or []})
    if probs:
        return {"error": "problems", "problems": probs}
    old_repo_names = set(old.get("repo_names") or [])
    repos = CREPO.read_repos(project)
    # Имена новых записей: имя каталога из URL, при конфликте — суффикс.
    chosen, creds_map = [], {}
    used = {n.casefold(): n for n in repos}
    for path in paths:
        p = meta.get(path) or {}
        repo_url = str(p.get("http_url_to_repo") or p.get("url") or "").strip()
        if not repo_url:
            return {"error": "project_url_required", "path": path}
        tail = CREPO.suggest_name(repo_url) or path.rsplit("/", 1)[-1]
        repo_name = next((n for n, rec in repos.items()
                          if GS.strip_userinfo(rec.get("url") or "") == GS.strip_userinfo(repo_url)), "")
        if not repo_name:
            repo_name = tail
            n = 2
            while repo_name.casefold() in used:
                repo_name = f"{tail}-{n}"
                n += 1
        used[repo_name.casefold()] = repo_name
        if repo_name in repos:
            # Повторный выбор не должен сбрасывать ручные маски, режим доступа и
            # разрешения, выставленные в карточке репозитория.
            rec = {**repos[repo_name], "url": repo_url, "provider": GS.PROVIDERS[1]}
            if not rec.get("branch"):
                rec["branch"] = str(p.get("default_branch") or "")
        else:
            rec = {**CREPO.blank(), "url": repo_url, "provider": GS.PROVIDERS[1],
                   "branch": str(p.get("default_branch") or ""), "models": False, "publish": False}
        repos[repo_name] = rec
        chosen.append((path, repo_name))
        if credentials:
            creds_map[repo_name] = credentials
    # Снять записи, которые группа добавляла раньше, но больше не выбраны. Чужие записи
    # с тем же именем не трогаем: группа помнит только свои `repo_names`.
    keep_names = {r[1] for r in chosen}
    shared = {n for other, rec in groups.items() if other != name
              for n in (rec.get("repo_names") or [])}
    for repo_name in old_repo_names - keep_names:
        if repo_name in shared:
            continue
        rec = repos.get(repo_name)
        if rec and rec.get("provider") == GS.PROVIDERS[1]:
            repos.pop(repo_name, None)
    res = CREPO.save(project, repos, repo_expect if isinstance(repo_expect, str) else "", creds_map)
    if not res.get("ok"):
        return {**res, "stage": "repos"}
    known = list(old.get("known") or [])
    known_set = {x.casefold() for x in known}
    for path, _ in chosen:
        if path.casefold() not in known_set:
            known.append(path)
            known_set.add(path.casefold())
    group.update(selected=[p for p, _ in chosen], known=known, repo_names=[n for _, n in chosen],
                 last_seen_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    groups[name] = group
    saved = save(project, groups, "")
    if not saved.get("ok"):
        return {**saved, "stage": "groups", "repos_saved": True}
    if isinstance(credentials, dict) and str(credentials.get("secret") or "").strip() \
            and str(credentials.get("secret")) != GS.MASK:
        # Токен нужен для следующего discovery; конфиг его не видит.
        remote = cred_remote(name, group.get("url") or "")
        try:
            GS.cred_save(project, {**GS.cred_load(project, remote), **credentials}, remote)
        except OSError:
            pass                        # репозитории уже сохранены; токен можно ввести снова
    return {"ok": True, "group": group, "repos": len(chosen), "digest": saved.get("digest", "")}
