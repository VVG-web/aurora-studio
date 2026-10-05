"""Gitea (и Forgejo): проверка входа, репозитория и прав через API v1.

Модуль ставится со страницы «Установка» в `~/.aurora/git-providers/gitea/` и грузится
движком (`git_sync.load_adapter`). Сеть — только через переданный помощник `http`: адаптер
не знает ни про сертификаты, ни про таймауты, это решает настройка проекта.

  check(cfg, http)  → {ok, code, detail, login, repo: {found, default_branch, can_push, …}}
  clone_urls(cfg)   → {http, ssh} — без сети
"""
from __future__ import annotations

import base64
import urllib.parse


def _split(repo: str) -> tuple:
    parts = [p for p in (repo or "").strip("/").split("/") if p]
    if len(parts) != 2:
        raise ValueError("у Gitea репозиторий записывается как владелец/имя")
    return parts[0], parts[1].removesuffix(".git")


def _headers(cfg: dict) -> dict:
    if cfg.get("auth") == "token" and cfg.get("secret"):
        return {"Authorization": "token " + cfg["secret"]}
    if cfg.get("auth") == "password" and cfg.get("user") and cfg.get("secret"):
        raw = f"{cfg['user']}:{cfg['secret']}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    return {}


def _code(status: int, err: str) -> str:
    if status == 0:
        return "cert" if err.startswith("cert:") else "network"
    return {401: "auth", 403: "forbidden", 404: "repo_not_found"}.get(status, "unknown")


def clone_urls(cfg: dict) -> dict:
    owner, name = _split(cfg.get("repo", ""))
    inst = (cfg.get("instance") or "").rstrip("/")
    host = urllib.parse.urlparse(inst).hostname or ""
    return {"http": f"{inst}/{owner}/{name}.git", "ssh": f"git@{host}:{owner}/{name}.git",
            "web": f"{inst}/{owner}/{name}"}


def check(cfg: dict, http) -> dict:
    out = {"ok": False, "code": "", "detail": "", "login": "", "repo": {}}
    try:
        owner, name = _split(cfg.get("repo", ""))
    except ValueError as e:
        out.update(code="bad_url", detail=str(e))
        return out
    base = (cfg.get("instance") or "").rstrip("/") + "/api/v1"
    headers = _headers(cfg)
    if headers:
        st, data, err, _h = http(base + "/user", headers)
        if st == 200 and isinstance(data, dict):
            out["login"] = data.get("login") or data.get("username") or ""
        else:
            out.update(code=_code(st, err), detail=f"вход: {err or st}")
            return out
    st, data, err, _h = http(f"{base}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}",
                             headers)
    if st != 200 or not isinstance(data, dict):
        out.update(code=_code(st, err), detail=f"репозиторий: {err or st}")
        return out
    perms = data.get("permissions") or {}
    out["repo"] = {"found": True, "default_branch": data.get("default_branch", ""),
                   "can_push": perms.get("push") if headers else None,
                   "private": data.get("private"), "empty": data.get("empty"),
                   "http_url": data.get("clone_url", ""), "ssh_url": data.get("ssh_url", ""),
                   "web_url": data.get("html_url", "")}
    if headers and perms and not perms.get("push"):
        out.update(code="forbidden", detail="вход принят, но права записи в репозиторий нет")
        return out
    out["ok"] = True
    return out
