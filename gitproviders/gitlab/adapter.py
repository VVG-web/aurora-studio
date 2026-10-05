"""GitLab (свой сервер или gitlab.com): проверка входа, проекта и прав через API v4.

Репозиторий — путь проекта целиком, с подгруппами: `группа/подгруппа/проект`. Токен —
личный, проектный или групповой (заголовок PRIVATE-TOKEN). Пароль в API GitLab не
принимается: с ним проверяется только git, а адаптер смотрит открытые сведения.

  check(cfg, http)  → {ok, code, detail, login, repo: {found, default_branch, can_push, …}}
  clone_urls(cfg)   → {http, ssh} — без сети
"""
from __future__ import annotations

import urllib.parse

DEVELOPER = 30          # уровень доступа, с которого GitLab пускает отправку


def _path(repo: str) -> str:
    path = (repo or "").strip("/").removesuffix(".git")
    if path.count("/") < 1:
        raise ValueError("у GitLab репозиторий записывается как группа/проект")
    return path


def _headers(cfg: dict) -> dict:
    if cfg.get("auth") == "token" and cfg.get("secret"):
        return {"PRIVATE-TOKEN": cfg["secret"]}
    return {}


def _code(status: int, err: str) -> str:
    if status == 0:
        return "cert" if err.startswith("cert:") else "network"
    return {401: "auth", 403: "forbidden", 404: "repo_not_found"}.get(status, "unknown")


def clone_urls(cfg: dict) -> dict:
    path = _path(cfg.get("repo", ""))
    inst = (cfg.get("instance") or "").rstrip("/")
    host = urllib.parse.urlparse(inst).hostname or ""
    return {"http": f"{inst}/{path}.git", "ssh": f"git@{host}:{path}.git", "web": f"{inst}/{path}"}


def _level(data: dict) -> int:
    perms = data.get("permissions") or {}
    levels = [(perms.get(k) or {}).get("access_level") or 0
              for k in ("project_access", "group_access")]
    return max(levels) if levels else 0


def check(cfg: dict, http) -> dict:
    out = {"ok": False, "code": "", "detail": "", "login": "", "repo": {}}
    try:
        path = _path(cfg.get("repo", ""))
    except ValueError as e:
        out.update(code="bad_url", detail=str(e))
        return out
    base = (cfg.get("instance") or "").rstrip("/") + "/api/v4"
    headers = _headers(cfg)
    if headers:
        st, data, err, _h = http(base + "/user", headers)
        if st == 200 and isinstance(data, dict):
            out["login"] = data.get("username", "")
        else:
            out.update(code=_code(st, err), detail=f"вход: {err or st}")
            return out
    st, data, err, _h = http(f"{base}/projects/{urllib.parse.quote(path, safe='')}", headers)
    if st != 200 or not isinstance(data, dict):
        out.update(code=_code(st, err), detail=f"проект: {err or st}")
        return out
    level = _level(data)
    out["repo"] = {"found": True, "default_branch": data.get("default_branch") or "",
                   "can_push": (level >= DEVELOPER) if headers else None,
                   "private": data.get("visibility") == "private",
                   "empty": data.get("empty_repo"),
                   "http_url": data.get("http_url_to_repo", ""),
                   "ssh_url": data.get("ssh_url_to_repo", ""), "web_url": data.get("web_url", "")}
    if headers and level and level < DEVELOPER:
        out.update(code="forbidden", detail="вход принят, но уровень доступа ниже Developer — "
                                            "отправлять нельзя")
        return out
    out["ok"] = True
    return out
