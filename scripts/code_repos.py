#!/usr/bin/env python3
"""code_repos.py — реестр репозиториев кода проекта (фреймворк «Аврора»).

Репозитории кода, чьи клоны лежат в `Code/<имя>/`, объявлены в `aurora.config.yaml`, блок `code_repos:` — файл
проекта, он в git, и по нему каждый участник получает тот же состав клонов (`code:sync`). Сами клоны в git проекта не
идут. Блок правится только из панели (раздел «Код»): форма шлёт слепок, который прочитала, и чужая правка между
чтением и записью называется расхождением (409), а не затирается; соседние разделы конфига остаются байт в байт.

    code_repos:
      order-intake:
        url: "https://git.example.com/team/order-intake.git"
        provider: "generic"
        branch: "main"
        pin: ""
        include: ""
        exclude: "**/generated/**, *.min.js"
        access: ""
        models: false
        publish: false

Поля: `url` — адрес клонирования без логина и пароля; `provider` — github, gitlab, bitbucket, gitea или generic, как у
«Git проекта»; `branch` — за какой веткой следить (пусто — ветка по умолчанию сервера); `pin` — закреплённая ревизия,
SHA или тег (пусто — идти за веткой); `include`, `exclude` — маски путей через запятую сверх умолчаний; `access` —
как до репозитория достать: пусто (как всегда), `internet`, `local` или `vpn:<профиль>` (проверка соединений);
`models` — можно ли отдавать фрагменты этого кода моделям (по умолчанию нельзя: решение владельца 30.10.2026);
`publish` — можно ли публиковать артефакты по этому коду (по умолчанию нельзя).

Вход в репозиторий — тот же механизм, что у «Git проекта» (`git_sync`): секрет лежит вне проекта, в
`~/.aurora/git/credentials.json` (права 600) под ключом — путём клона `Code/<имя>`; наружу отдаётся только маска.
Токен не пишется ни в конфиг, ни в адрес, ни в командную строку git.

Модуль только читает и пишет реестр; клонирует и обновляет `code_sync.py` (`code:sync`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import unicodedata
import urllib.parse
from pathlib import Path

import git_sync as GS
from aurora_common import CODE_DIR, FOLDER_GUIDE

CONFIG = "aurora.config.yaml"
KEY = "code_repos"
FIELDS = ("url", "provider", "branch", "pin", "include", "exclude", "access", "models", "publish")
BOOL_FIELDS = ("models", "publish")
NAME_MAX = 64
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Имена, которые Windows не даёт папке ни с каким расширением: клон с таким именем не выгрузится у половины команды.
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
ACCESS_RX = re.compile(r"^(?:internet|local|vpn:[A-Za-z0-9_.-]{1,64})?$")
PIN_RX = re.compile(r"^[0-9a-fA-F]{7,40}$")
_TRUE = ("true", "yes", "on", "1", "да")
_FALSE = ("false", "no", "off", "0", "нет", "")
_HEAD_RE = re.compile(r"^code_repos[ \t]*:[ \t]*(?:#.*)?$")
_LOCK = threading.Lock()                     # два окна панели не пишут блок одновременно
_MEMO: dict = {}                             # разбор реестра по (путь, время правки): модель спрашивает на каждый файл


def config_path(project) -> Path:
    return Path(project) / CONFIG


def clone_path(project, name: str) -> Path:
    """Папка клона репозитория `name`: `Code/<имя>` от корня проекта."""
    return Path(project) / CODE_DIR / name


def _bool(value) -> bool | None:
    """«true/false» конфига → да/нет; непонятное значение — None (замечание, а не молчаливое «нет»)."""
    if isinstance(value, bool):
        return value
    low = str(value if value is not None else "").strip().strip("\"'").lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return None


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        inner = raw[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if raw[0] == '"' else inner.replace("''", "'")
    return re.sub(r"\s+#.*$", "", raw).strip()


def _block_span(lines: list) -> tuple | None:
    """(первая строка, строка после последней) блока `code_repos:` верхнего уровня; нет — None.
    Пустые строки после блока — разделитель разделов, а не его часть."""
    start = next((i for i, ln in enumerate(lines) if _HEAD_RE.match(ln.rstrip("\r"))), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
        end += 1
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def parse(text: str) -> dict:
    """{имя: запись} из текста конфига — так же, как читает движок. Записи — в порядке файла; поле, которого нет, —
    умолчание. Значения `models`/`publish` — да/нет, непонятное значение — строкой как есть (её назовёт `check`)."""
    text = text[1:] if text.startswith("﻿") else text
    lines = text.split("\n")
    span = _block_span(lines)
    if not span:
        return {}
    repos: dict = {}
    cur = None
    for line in lines[span[0] + 1:span[1]]:
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        m = re.match(r"\s*([^\s:#][^:#]*?)\s*:(?:\s+(.*))?$", line)
        if not m:
            continue
        key, value = _unquote(m.group(1)), m.group(2) or ""
        if indent <= 2 and not value.strip():
            cur = key
            repos[cur] = blank()
        elif cur is not None and key in FIELDS:
            v = _unquote(value)
            if key in BOOL_FIELDS:
                b = _bool(v)
                repos[cur][key] = b if b is not None else v
            else:
                repos[cur][key] = v
    return repos


def blank() -> dict:
    return {f: (False if f in BOOL_FIELDS else "") for f in FIELDS}


def read_repos(project=".") -> dict:
    """Реестр проекта (`parse` файла конфига). Нет файла или блока — пусто."""
    path = config_path(project)
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path.resolve()), st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    if _MEMO.get("key") == key:
        return json.loads(_MEMO["repos"])
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    repos = parse(text)
    _MEMO.update(key=key, repos=json.dumps(repos))
    return repos


def find(repos: dict, name: str) -> str:
    """Имя записи реестра для имени папки — без учёта регистра (macOS и Windows); нет — пусто."""
    low = (name or "").casefold()
    return next((n for n in repos if n.casefold() == low), "")


def models_allowed(project, name: str) -> bool:
    """Можно ли отдавать модели код репозитория `name`: только при `models: true` в реестре."""
    repos = read_repos(project)
    rec = repos.get(find(repos, name)) or {}
    return rec.get("models") is True


def access_needs(rec: dict) -> dict:
    """Что нужно репозиторию по сети (поле `access`): {profile, internet}. Пусто и `local` — сеть не проверяется."""
    access = str((rec or {}).get("access") or "").strip()
    if access.startswith("vpn:"):
        return {"profile": access[4:], "internet": False}
    return {"profile": "", "internet": access == "internet"}


def net_needs(project) -> dict:
    """Вычисляемый источник «код» для проверки соединений: профили и интернет всех репозиториев реестра —
    {profiles, internet}. Для предзапуска `code:sync` целиком берётся самое строгое; по одному репозиторию решает
    сама команда (недоступный пропускается, остальные идут)."""
    profiles, internet = set(), False
    for rec in read_repos(project).values():
        need = access_needs(rec)
        if need["profile"]:
            profiles.add(need["profile"])
        internet = internet or need["internet"]
    return {"profiles": sorted(profiles), "internet": internet}


def digest(repos: dict) -> str:
    """Слепок реестра так, как его читает движок: комментарии, пустые строки и порядок записей изменением не считаются."""
    norm = {n: {f: rec.get(f) for f in FIELDS} for n, rec in (repos or {}).items()}
    return hashlib.sha256(json.dumps(norm, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def masks(value) -> list:
    """Маски путей из строки через запятую (или списка): пустые отброшены."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def name_problem(name: str) -> str:
    """Код замечания к имени репозитория (= имени папки `Code/<имя>`); пусто — годится. Правила трёх систем сразу и
    ключа YAML: латиница, цифры, «.», «_», «-», с буквы или цифры, не длиннее 64."""
    n = str(name or "")
    if not n.strip():
        return "name_empty"
    if len(n) > NAME_MAX:
        return "name_long"
    if not NAME_RX.match(n) or n.endswith("."):
        return "name_chars"
    if n.split(".")[0].lower() in RESERVED:
        return "name_reserved"
    if n.casefold() in (FOLDER_GUIDE.casefold(), ".gitkeep"):
        return "name_reserved"
    return ""


def _one_line(value) -> bool:
    text = str(value if value is not None else "")
    return not any(unicodedata.category(c) in ("Cc", "Zl", "Zp") and c != "\t" for c in text)


def normalize(name: str, rec) -> tuple:
    """(запись с полями по умолчанию, замечания [{name, field, code}]). Логин в адресе убирается молча (так даёт строку
    клонирования Bitbucket — логин берут во «Вход»), пароль в адресе — замечание: он не хранится в конфиге."""
    rec = rec if isinstance(rec, dict) else {}
    out, probs = blank(), []

    def bad(field, code):
        probs.append({"name": name, "field": field, "code": code})

    for f in FIELDS:
        v = rec.get(f)
        if f in BOOL_FIELDS:
            b = _bool(v)
            if b is None:
                bad(f, "bool")
            out[f] = bool(b)
            continue
        v = "" if v is None else str(v).strip()
        if not _one_line(v):
            bad(f, "multiline")
            v = ""
        out[f] = v
    url = out["url"]
    if url != GS.strip_userinfo(url):
        if GS.url_userinfo(url)[1]:
            bad("url", "creds_in_url")
        url = GS.strip_userinfo(url)
    out["url"] = url
    if not url:
        bad("url", "required")
    elif not GS.is_url(url):
        bad("url", "bad_url")
    prov = out["provider"].lower() or (GS.guess_provider(url) if url else "generic")
    if prov not in GS.PROVIDERS:
        bad("provider", "unknown")
        prov = "generic"
    out["provider"] = prov
    if out["branch"] and not GS.valid_branch(out["branch"]):
        bad("branch", "branch")
    if out["pin"] and not (PIN_RX.match(out["pin"]) or GS.valid_branch(out["pin"])):
        bad("pin", "pin")
    for f in ("include", "exclude"):
        for m in masks(out[f]):
            mm = m.replace("\\", "/")
            if mm.startswith("/") or re.match(r"^[A-Za-z]:", mm) or ".." in mm.split("/"):
                bad(f, "mask")
                break
    if not ACCESS_RX.match(out["access"]):
        bad("access", "access")
    return out, probs


def check(repos: dict) -> tuple:
    """(нормализованный реестр, замечания). Имена — по правилам трёх систем и уникальны без учёта регистра."""
    out, probs, seen = {}, [], set()
    for name, rec in (repos or {}).items():
        name = str(name)
        why = name_problem(name)
        if why:
            probs.append({"name": name, "field": "name", "code": why})
        elif name.casefold() in seen:
            probs.append({"name": name, "field": "name", "code": "name_duplicate"})
        seen.add(name.casefold())
        out[name], p = normalize(name, rec)
        probs += p
    return out, probs


def _yaml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def block_lines(repos: dict) -> list:
    """Блок `code_repos:` строками без окончаний: записи по имени, поля — по `FIELDS`. Читается обратно `parse`."""
    lines = [KEY + ":"]
    for name in sorted(repos, key=str.casefold):
        rec = repos[name]
        lines.append(f"  {name}:")
        for f in FIELDS:
            v = rec.get(f)
            lines.append(f"    {f}: " + (("true" if v is True else "false") if f in BOOL_FIELDS
                                         else _yaml_str(v or "")))
    return lines


def write_block(config_text: str, repos: dict) -> str:
    """Текст конфига с переписанным блоком `code_repos:`; всё остальное — байт в байт: метка порядка байтов, окончания
    строк, пустые строки и комментарии вне блока. Пустой реестр — блок снимается. Запись идемпотентна."""
    bom = "﻿" if config_text.startswith("﻿") else ""
    lines = config_text[len(bom):].split("\n")
    span = _block_span(lines)
    block = block_lines(repos) if repos else []
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
        # блок снят: пустые строки, отделявшие его от предыдущего раздела, уходят вместе с ним
        while start > 0 and not lines[start - 1].strip():
            start -= 1
        while start == 0 and len(rest) > 1 and not rest[0].strip():
            rest = rest[1:]
        return bom + "\n".join(lines[:start] + rest)
    return bom + "\n".join(lines[:start] + [ln + cr for ln in block] + rest)


# ---------------------------------------------------------------- вход: секреты вне проекта

def server_of(rec: dict) -> dict:
    """Настройка сервера для `git_sync` (вход, сертификат): адрес клонирования как есть."""
    url = rec.get("url") or ""
    inst = ""
    if GS.URL_RX.match(url):
        u = urllib.parse.urlparse(url)
        inst = f"{u.scheme}://{u.netloc}"
    return {"provider": rec.get("provider") or "generic", "instance": inst, "repo": url, "remote": "origin",
            "tls": {"verify": True, "ca_file": ""}}


def creds_of(project, name: str) -> dict:
    return GS.cred_load(clone_path(project, name))


def creds_masked(project, name: str) -> dict:
    return GS.cred_masked(clone_path(project, name))


def _merge_creds(project, name: str, given, old_rec: dict | None, new_rec: dict) -> tuple:
    """(вход, замечания) репозитория по присланному формой. Маска — «не трогали», пусто — «убрать». Сохранённый
    секрет принадлежит прежнему серверу: сменился хост адреса — секрет не переезжает, его вводят заново (иначе подмена
    адреса в форме унесла бы токен на чужой сервер)."""
    target = clone_path(project, name)
    c, probs = GS.cred_merge(target, given if isinstance(given, dict) else {}, None, None)
    carried = not (isinstance(given, dict) and "secret" in given and str(given.get("secret")) != GS.MASK)
    if carried and c.get("secret") and old_rec is not None \
            and GS.hostport(old_rec.get("url") or "") != GS.hostport(new_rec.get("url") or ""):
        c["secret"] = ""
        if c["auth"] in ("token", "password"):
            probs = [p for p in probs if p.get("field") != "secret"] + [{"field": "secret", "code": "required"}]
    probs += GS._token_user(server_of(new_rec), c, "")
    return c, [{"name": name, **p} for p in probs]


def save(project, repos, expect: str = "", creds=None) -> dict:
    """Записать реестр целиком и вход репозиториев. → {ok, digest} | {conflict, …состояние} | {error, problems}.

    `expect` — слепок реестра, который видела форма (`state` → `digest`); на диске другой — расхождение, ничего не
    пишется. Пустой слепок — запись без сверки (так делает только «Записать моё поверх»). `creds` — {имя: вход}
    только тех репозиториев, чей вход правили; у удалённого из реестра репозитория вход стирается. Есть замечания —
    не пишется ничего: ни конфиг, ни секреты."""
    path = config_path(project)
    if not isinstance(expect, str):
        return {"error": "bad_expect"}
    if not isinstance(repos, dict):
        return {"error": "bad_repos"}
    if not path.is_file():
        return {"error": "no_config"}
    clean, probs = check(repos)
    with _LOCK:
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                old_text = fh.read()
        except UnicodeDecodeError:
            return {"error": "config_encoding"}
        except OSError:
            return {"error": "config_unreadable", "failed": True}
        old = parse(old_text)
        if expect and expect != digest(old):
            return {"conflict": True, "error": "conflict", **state(project)}
        given = creds if isinstance(creds, dict) else {}
        merged = {}
        for name, rec in clean.items():
            if name in given:
                c, p = _merge_creds(project, name, given[name], old.get(find(old, name)), rec)
                merged[name] = c
                probs += p
        if probs:
            return {"error": "problems", "problems": probs}
        new_text = write_block(old_text, clean)
        if new_text != old_text:
            try:
                import aurora_fs
                aurora_fs.write_atomic(str(path), new_text, newline="", makedirs=False)
            except OSError:
                return {"error": "config_unwritable", "failed": True}
        for name, c in merged.items():
            GS.cred_save(clone_path(project, name), c)
        for gone in [n for n in old if not find(clean, n)]:
            GS.cred_save(clone_path(project, gone), None)
        return {"ok": True, "digest": digest(parse(new_text)), "repos": len(clean)}


# ---------------------------------------------------------------- для панели

def suggest_name(text: str) -> str:
    """Имя папки по адресу репозитория: последний сегмент пути без `.git`, приведённый к правилам имени."""
    parsed = GS.parse_clone(text) if text else {}
    url = parsed.get("url") or ""
    tail = re.split(r"[/:]", url.rstrip("/"))[-1] if url else ""
    tail = tail[:-4] if tail.lower().endswith(".git") else tail
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-.")[:NAME_MAX].rstrip(".")
    return name if name and not name_problem(name) else ""


def parse_clone(text: str) -> dict:
    """Строка `git clone …` (или адрес) → провайдер, адрес, ветка, вход — как у «Git проекта» — и имя папки.
    Ничего не пишет."""
    out = GS.parse_clone(text)
    if out.get("ok"):
        out["name"] = suggest_name(text)
    return out


def check_remote(project, name: str, rec, given=None) -> dict:
    """Проверить подключение по записи формы (в том числе не сохранённой): `ls-remote` с тем же входом, что возьмёт
    `code:sync`. Ничего не сохраняет: вставленный ключ живёт во временном файле. → {ok, branches, problem}."""
    rec, probs = normalize(name, rec)
    if any(p["field"] == "url" for p in probs):
        return {"ok": False, "problems": probs}
    repos = read_repos(project)
    saved = repos.get(find(repos, name))
    if not GS.has_git():
        return {"ok": False, "problem": GS.problem("no_git")}
    with tempfile.TemporaryDirectory(prefix="aurora-codecheck-") as tmp:
        if given is not None:
            given = dict(given)
            text = str(given.pop("ssh_key_text", "") or "").strip()
            c = _merge_creds(project, name, given, saved, rec)[0]
            if text:
                key = Path(tmp) / "check.key"
                key.write_text(text.replace("\r\n", "\n").strip() + "\n", encoding="utf-8")
                os.chmod(key, 0o600)
                c["ssh_key"] = str(key)
        else:
            c = creds_of(project, name)
            if saved is not None and GS.hostport(saved.get("url") or "") != GS.hostport(rec["url"]):
                c = {**c, "secret": ""}         # ключ прежнего сервера на новый адрес не уходит
        rc, o, e = GS.run_git(tmp, ["ls-remote", "--heads", rec["url"]], server_of(rec), c, 60, network=True)
    heads = [ln.split("refs/heads/", 1)[1] for ln in o.splitlines() if "refs/heads/" in ln]
    if rc:
        tail = (o + "\n" + e).strip()
        return {"ok": False, "branches": [], "problem": GS.problem(GS.classify(tail), tail)}
    want = rec["branch"]
    if want and heads and want not in heads:
        return {"ok": False, "branches": heads[:50], "problem": GS.problem("remote_branch_missing")}
    return {"ok": True, "branches": heads[:50], "empty": not heads}


def state(project) -> dict:
    """Всё для раздела «Код»: реестр с маской входа, слепок, замечания к записанному, варианты полей."""
    repos = read_repos(project)
    clean, probs = check(repos)
    rows = []
    for name, rec in clean.items():
        rows.append({"name": name, **rec, "path": f"{CODE_DIR}/{name}",
                     "cloned": (clone_path(project, name) / ".git").exists(),
                     "credentials": creds_masked(project, name)})
    return {"repos": rows, "digest": digest(repos), "problems": probs,
            "options": {"providers": list(GS.PROVIDERS), "auths": list(GS.AUTHS)}}


def stray_dirs(project) -> list:
    """Папки в `Code/` без записи в реестре (их назовёт `kit:doctor`)."""
    base = Path(project) / CODE_DIR
    repos = read_repos(project)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".") and not find(repos, p.name))
