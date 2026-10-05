"""Bitbucket: свой сервер (Server / Data Center, REST 1.0) и bitbucket.org (API 2.0).

Свой сервер: репозиторий — `КЛЮЧ_ПРОЕКТА/репозиторий` (личный — `~логин/репозиторий`),
вход — HTTP access token (Bearer) или логин с паролем. Адрес клонирования по HTTP у
сервера свой: `<сервер>/scm/<ключ>/<репозиторий>.git`, по SSH — порт 7999.
bitbucket.org: `рабочее-пространство/репозиторий`, токен доступа — Bearer, пароль
приложения — с логином.

  check(cfg, http)  → {ok, code, detail, login, repo: {found, default_branch, can_push, …}}
  clone_urls(cfg)   → {http, ssh} — без сети
"""
from __future__ import annotations

import base64
import urllib.parse

CLOUD_API = "https://api.bitbucket.org/2.0"


def _cloud(cfg: dict) -> bool:
    host = urllib.parse.urlparse(cfg.get("instance") or "").hostname or ""
    return host in ("bitbucket.org", "www.bitbucket.org", "api.bitbucket.org")


def _split(repo: str) -> tuple:
    parts = [p for p in (repo or "").strip("/").split("/") if p]
    if len(parts) != 2:
        raise ValueError("у Bitbucket репозиторий записывается как проект/репозиторий")
    return parts[0], parts[1].removesuffix(".git")


def _headers(cfg: dict) -> dict:
    if cfg.get("auth") == "token" and cfg.get("secret"):
        return {"Authorization": "Bearer " + cfg["secret"]}
    if cfg.get("auth") == "password" and cfg.get("user") and cfg.get("secret"):
        raw = f"{cfg['user']}:{cfg['secret']}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    return {}


def _code(status: int, err: str) -> str:
    if status == 0:
        return "cert" if err.startswith("cert:") else "network"
    return {401: "auth", 403: "forbidden", 404: "repo_not_found"}.get(status, "unknown")


def clone_urls(cfg: dict) -> dict:
    key, slug = _split(cfg.get("repo", ""))
    if _cloud(cfg):
        return {"http": f"https://bitbucket.org/{key}/{slug}.git",
                "ssh": f"git@bitbucket.org:{key}/{slug}.git",
                "web": f"https://bitbucket.org/{key}/{slug}"}
    inst = (cfg.get("instance") or "").rstrip("/")
    host = urllib.parse.urlparse(inst).hostname or ""
    return {"http": f"{inst}/scm/{key.lower()}/{slug}.git",
            "ssh": f"ssh://git@{host}:7999/{key.lower()}/{slug}.git",
            "web": f"{inst}/projects/{key}/repos/{slug}"}


def _links(data: dict) -> dict:
    clone = {}
    for c in ((data.get("links") or {}).get("clone") or []):
        if isinstance(c, dict):
            clone[c.get("name", "")] = c.get("href", "")
    return clone


def check(cfg: dict, http) -> dict:
    out = {"ok": False, "code": "", "detail": "", "login": "", "repo": {}}
    try:
        key, slug = _split(cfg.get("repo", ""))
    except ValueError as e:
        out.update(code="bad_url", detail=str(e))
        return out
    headers = _headers(cfg)
    if _cloud(cfg):
        st, data, err, _h = http(f"{CLOUD_API}/repositories/{urllib.parse.quote(key)}/"
                                 f"{urllib.parse.quote(slug)}", headers)
        if st != 200 or not isinstance(data, dict):
            out.update(code=_code(st, err), detail=f"репозиторий: {err or st}")
            return out
        links = _links(data)
        out["repo"] = {"found": True,
                       "default_branch": (data.get("mainbranch") or {}).get("name", ""),
                       "can_push": None, "private": data.get("is_private"),
                       "http_url": links.get("https", ""), "ssh_url": links.get("ssh", ""),
                       "web_url": ((data.get("links") or {}).get("html") or {}).get("href", "")}
        out["ok"] = True
        return out
    base = (cfg.get("instance") or "").rstrip("/") + "/rest/api/1.0"
    project = f"users/{key[1:]}" if key.startswith("~") else f"projects/{key}"
    st, data, err, hdrs = http(f"{base}/{project}/repos/{urllib.parse.quote(slug)}", headers)
    if st != 200 or not isinstance(data, dict):
        out.update(code=_code(st, err), detail=f"репозиторий: {err or st}")
        return out
    out["login"] = hdrs.get("x-ausername", "")
    links = _links(data)
    branch = ""
    st2, d2, _e2, _h2 = http(f"{base}/{project}/repos/{urllib.parse.quote(slug)}/default-branch",
                             headers)
    if st2 != 200:
        st2, d2, _e2, _h2 = http(f"{base}/{project}/repos/{urllib.parse.quote(slug)}/branches/default",
                                 headers)
    if st2 == 200 and isinstance(d2, dict):
        branch = d2.get("displayId", "")
    out["repo"] = {"found": True, "default_branch": branch, "can_push": None,
                   "private": not data.get("public", False),
                   "http_url": links.get("http", ""), "ssh_url": links.get("ssh", ""),
                   "web_url": (((data.get("links") or {}).get("self") or [{}])[0] or {}).get("href", "")}
    out["ok"] = True
    return out
