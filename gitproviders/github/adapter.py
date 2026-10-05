"""GitHub (github.com и GitHub Enterprise Server): проверка входа, репозитория и прав через REST API.

github.com — API на `api.github.com`; свой сервер GitHub Enterprise — `<сервер>/api/v3`.
Репозиторий — `владелец/имя`. Вход — personal access token (классический или
fine-grained с правом Contents: read and write), заголовок `Authorization: Bearer`.
Пароль GitHub для git и API не принимает с 2021 года: с ним проверяется только сам git.

Модуль ставится со страницы «Установка» в `~/.aurora/git-providers/github/` и грузится
движком (`git_sync.load_adapter`). Сеть — только через переданный помощник `http`.

  check(cfg, http)  → {ok, code, detail, login, repo: {found, default_branch, can_push, …}}
  clone_urls(cfg)   → {http, ssh} — без сети
"""
from __future__ import annotations

import urllib.parse

PUBLIC = "https://github.com"


def _instance(cfg: dict) -> str:
    return (cfg.get("instance") or PUBLIC).rstrip("/")


def _public(cfg: dict) -> bool:
    host = urllib.parse.urlparse(_instance(cfg)).hostname or ""
    return host in ("github.com", "www.github.com", "api.github.com")


def _api(cfg: dict) -> str:
    return "https://api.github.com" if _public(cfg) else _instance(cfg) + "/api/v3"


def _split(repo: str) -> tuple:
    parts = [p for p in (repo or "").strip("/").split("/") if p]
    if len(parts) != 2:
        raise ValueError("у GitHub репозиторий записывается как владелец/имя")
    return parts[0], parts[1].removesuffix(".git")


def _headers(cfg: dict) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if cfg.get("auth") == "token" and cfg.get("secret"):
        h["Authorization"] = "Bearer " + cfg["secret"]
    return h


def _code(status: int, err: str) -> str:
    if status == 0:
        return "cert" if err.startswith("cert:") else "network"
    return {401: "auth", 403: "forbidden", 404: "repo_not_found"}.get(status, "unknown")


def clone_urls(cfg: dict) -> dict:
    owner, name = _split(cfg.get("repo", ""))
    inst = _instance(cfg)
    host = urllib.parse.urlparse(inst).hostname or "github.com"
    return {"http": f"{inst}/{owner}/{name}.git", "ssh": f"git@{host}:{owner}/{name}.git",
            "web": f"{inst}/{owner}/{name}"}


def check(cfg: dict, http) -> dict:
    out = {"ok": False, "code": "", "detail": "", "login": "", "repo": {}}
    try:
        owner, name = _split(cfg.get("repo", ""))
    except ValueError as e:
        out.update(code="bad_url", detail=str(e))
        return out
    base = _api(cfg)
    headers = _headers(cfg)
    signed = "Authorization" in headers
    if signed:
        st, data, err, _h = http(base + "/user", headers)
        if st == 200 and isinstance(data, dict):
            out["login"] = data.get("login", "")
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
                   "can_push": perms.get("push") if signed else None,
                   "private": data.get("private"),
                   "http_url": data.get("clone_url", ""), "ssh_url": data.get("ssh_url", ""),
                   "web_url": data.get("html_url", "")}
    if signed and perms and not perms.get("push"):
        out.update(code="forbidden", detail="вход принят, но права записи в репозиторий нет")
        return out
    out["ok"] = True
    return out
