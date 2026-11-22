#!/usr/bin/env python3
"""aurora_guard.py — общий слой доступа панели Авроры.

Здесь живут три вещи, которые одинаково нужны панели, удалённому MCP-серверу и
управлению токенами:

* `RateLimiter` — ограничитель частоты в памяти процесса;
* доверие к прокси и разбор адреса клиента (`trusted_proxy`, `client_ip`,
  `admin_network`);
* `AuthContext`/`authenticate`/`require_*`/`remote_allowed` — единое прочтение
  заголовков и точный список путей, доступных удалённому токену;
* `audit_write` — журнал без секретов, тел и строк запроса.

Модуль не зависит от `aurora_remote_mcp.py`: его импортируют и панель, и сервер.
Имена из контракта v2 (раздел 10.7) заморожены.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import mcp_tokens as MT

try:                                            # в тестах движка может не быть рядом
    import aurora_common as AC
except Exception:                               # noqa: BLE001
    AC = None

# Точный список контракта 10.7. Ни префиксов, ни звёздочек: путь сравнивается целиком.
REMOTE_READ_PATHS = frozenset({
    "/api/ping", "/api/live", "/api/jobs", "/api/tasks", "/api/threads",
    "/api/state", "/api/activity", "/api/history", "/api/history/run",
    "/api/run/steps", "/api/run/logs", "/api/run/file", "/api/run/errors",
    "/api/job", "/api/route/live", "/api/health", "/api/agent", "/api/net/state",
    "/api/mcp/kit", "/api/scenarios", "/api/artifacts", "/api/report", "/api/kinds",
    "/api/cron",
})
REMOTE_WRITE_PATHS = frozenset({
    "/api/task/check", "/api/run", "/api/job/stop", "/api/task/stop",
    "/api/route/run", "/api/route/stop", "/api/ask/control", "/api/make/control",
    "/api/cron/start", "/api/cron/stop", "/api/cron/toggle",
})
REMOTE_ADMIN_PATHS = frozenset({
    "/api/cron/save", "/api/cron/delete", "/api/restart",
})
REMOTE_OTHER_POST = frozenset({
    "/api/diagnostics", "/api/mcp/confirm/request",
})

REMOTE_ALLOWED = (
    {("GET", path) for path in REMOTE_READ_PATHS}
    | {("POST", path) for path in REMOTE_WRITE_PATHS | REMOTE_ADMIN_PATHS | REMOTE_OTHER_POST}
)

_FORBIDDEN_AUDIT_KEYS = frozenset({
    "token", "secret", "hash", "body", "prompt", "prompts", "messages", "message_body",
    "request", "response", "headers", "authorization", "auth", "cookie", "set-cookie",
    "payload", "content", "args", "query", "url", "password", "key", "api_key",
})
_TOKEN_TEXT = re.compile(r"aur_(?:kn|rm)_[A-Za-z0-9_-]+")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s#]*")


class AuthError(Exception):
    """Отказ доступа: HTTP-статус, код контракта и человеческий текст."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


@dataclass(frozen=True)
class AuthContext:
    kind: str
    token_id: str = ""
    user: str = ""
    scopes: frozenset[str] = frozenset()
    projects: tuple[str, ...] = ()
    client_ip: str = ""
    vpn: bool = False
    mode: str = "safe"
    source: str = ""


def _header(headers, name: str) -> str:
    """Значение заголовка без зависимости от регистра и типа отображения."""
    if headers is None:
        return ""
    try:
        value = headers.get(name)
    except Exception:                           # noqa: BLE001 — чужое отображение не роняет разбор
        value = None
    if value is not None:
        return str(value)
    try:
        items = headers.items()
    except Exception:                           # noqa: BLE001
        return ""
    for key, item in items:
        if str(key).lower() == name.lower():
            return str(item)
    return ""


def _compare(one: str, two: str) -> bool:
    try:
        return hmac.compare_digest(one.encode("utf-8"), two.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


def _as_ip(value):
    text = str(value or "").strip()
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    text = text.split("%", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def trusted_proxy(peer_ip) -> bool:
    """Прокси доверяем только на петле: заголовкам чужого адреса веры нет."""
    address = _as_ip(peer_ip)
    return bool(address and address.is_loopback)


def client_ip(headers, peer_ip) -> str:
    """Адрес клиента: первый `X-Forwarded-For` только от петлевого прокси."""
    peer = str(peer_ip or "").strip()
    forwarded = _header(headers, "X-Forwarded-For").strip()
    if forwarded and trusted_proxy(peer):
        first = forwarded.split(",", 1)[0].strip()
        if first and _as_ip(first) is not None:
            return first
    return peer


def admin_network(headers, peer_ip) -> bool:
    """Админ-сеть: заголовок `X-Aurora-Admin-Network: 1` принимается только от петли."""
    return trusted_proxy(peer_ip) \
        and _header(headers, "X-Aurora-Admin-Network").strip() == "1"


def _sanitize(value, depth: int = 0):
    if depth > 6:
        return "[cut]"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_AUDIT_KEYS:
                continue
            out[str(key)[:80]] = _sanitize(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        text = _TOKEN_TEXT.sub("[redacted]", value)
        text = _URL_QUERY.sub(r"\1?[redacted]", text)
        text = text.replace("\r", " ").replace("\n", " ")
        return text[:2000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize(str(value), depth + 1)


def audit_write(path, record) -> None:
    """Дописать одну строку JSON. Права `0600`, без секретов, тел и строк запроса."""
    if not isinstance(record, dict):
        record = {"record": record}
    payload = _sanitize(record)
    payload.setdefault("at", MT._utc())
    target = os.path.abspath(os.path.expanduser(os.fspath(path)))
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def _mcp_json_path(kit=None) -> str:
    if kit:
        return os.path.join(os.path.expanduser(os.fspath(kit)), "local", "mcp.json")
    if AC is not None:
        try:
            root = AC.kit_root()
            if root:
                return os.path.join(str(root), "local", "mcp.json")
        except Exception:                       # noqa: BLE001, S110
            pass
    return ""


def _real_mcp_json() -> str:
    if AC is not None:
        try:
            root = AC.kit_root()
            if root:
                return os.path.realpath(os.path.join(str(root), "local", "mcp.json"))
        except Exception:                       # noqa: BLE001, S110
            pass
    return ""


def _mode_from_file(kit=None) -> str:
    """Глобальный режим из `<кит>/local/mcp.json`: нет или битый `auroraRemote` — `safe`."""
    path = _mcp_json_path(kit)
    if (os.environ.get("AURORA_TESTS_ISOLATED") and path
            and os.path.normcase(os.path.realpath(path))
            == os.path.normcase(_real_mcp_json())):
        return "safe"                           # тест не читает личную настройку машины
    if not path or not os.path.isfile(path):
        return "safe"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return "safe"
    if not isinstance(data, dict):
        return "safe"
    remote = data.get("auroraRemote")
    if not isinstance(remote, dict):
        return "safe"
    mode = str(remote.get("mode") or "").strip().lower()
    return mode if mode in ("safe", "full") else "safe"


def effective_mode(project="", *, kit=None) -> str:
    """Действующий режим удалённого доступа: `safe`/`full` с учётом проекта.

    Один и тот же файл хранит общий режим и переопределения проектов; проект важнее
    общего значения. Любая неизвестность — `safe`: опасное действие не разрешается
    по недописанному или битому файлу. Без `aurora_common` остаётся глобальный разбор.
    """
    if AC is not None:
        try:
            mode, _source = AC.kit_mcp_mode(str(project or ""), kit=kit)
            return mode if mode in ("safe", "full") else "safe"
        except Exception:                       # noqa: BLE001 — битый файл не разрешает опасное
            return "safe"
    return _mode_from_file(kit)


def session_context(headers=None, peer_ip="", *, kit=None) -> AuthContext:
    """Контекст локальной сессии панели: все права, проекты `*` только в памяти."""
    return AuthContext(
        kind="session",
        scopes=frozenset(MT.SCOPES),
        projects=("*",),
        client_ip=client_ip(headers, peer_ip),
        vpn=admin_network(headers, peer_ip),
        mode=effective_mode(kit=kit),
        source="session",
    )


def authenticate(headers, peer_ip="", *, session_token="", kit=None):
    """Прочитать `X-Aurora-Token`, затем `Authorization: Bearer`. → контекст или None.

    Сессия панели сверяется с переданным токеном; иначе `mcp_tokens.verify`. Битая
    запись реестра, истёкший или отозванный токен дают None: удалённый доступ
    закрывается, локальная сессия продолжает работать.
    """
    presented = _header(headers, "X-Aurora-Token").strip()
    if not presented:
        authz = _header(headers, "Authorization").strip()
        if authz.lower().startswith("bearer "):
            presented = authz[7:].strip()
    if not presented:
        return None
    if session_token and _compare(presented, str(session_token)):
        return session_context(headers, peer_ip, kit=kit)
    try:
        record = MT.verify(presented, kit=kit)
    except Exception:                           # noqa: BLE001 — реестр битый: fail closed
        record = None
    if not record:
        return None
    # `last_used_ip` пишется только когда адрес пришёл от доверенного прокси.
    forwarded = _header(headers, "X-Forwarded-For").strip()
    last_ip = client_ip(headers, peer_ip) if (forwarded and trusted_proxy(peer_ip)) else ""
    try:
        MT.touch(str(record.get("id") or ""), last_ip, kit=kit)
    except Exception:                           # noqa: BLE001, S110 — отметка не должна мешать работе
        pass
    return AuthContext(
        kind=str(record.get("kind") or ""),
        token_id=str(record.get("id") or ""),
        user=str(record.get("user") or ""),
        scopes=frozenset(record.get("scopes") or ()),
        projects=tuple(record.get("projects") or ()),
        client_ip=client_ip(headers, peer_ip),
        vpn=admin_network(headers, peer_ip),
        mode=effective_mode(kit=kit),
        source="header",
    )


def scope_ok(ctx, scope) -> bool:
    return bool(ctx is not None and scope in (ctx.scopes or ()))


def require_scope(ctx, scope) -> None:
    if ctx is None:
        raise AuthError(401, "token", "нужен токен доступа")
    if not scope_ok(ctx, scope):
        raise AuthError(403, "scope", f"токену не хватает права {scope}")


def project_ok(ctx, project) -> bool:
    if ctx is None:
        return False
    wanted = str(project or "").strip()
    if not wanted or wanted == "*":
        return False
    if "*" in (ctx.projects or ()):
        return True
    return MT.check_project({"projects": list(ctx.projects or ())}, wanted)


def require_project(ctx, project) -> None:
    wanted = str(project or "").strip()
    if not wanted or wanted == "*":
        raise AuthError(400, "invalid_project", "проект не указан")
    if ctx is None:
        raise AuthError(401, "token", "нужен токен доступа")
    if not project_ok(ctx, wanted):
        raise AuthError(403, "project", f"проект {wanted} не входит в права токена")


def visible_projects(ctx, names) -> list:
    rows = list(names or [])
    if ctx is None:
        return []
    if ctx.kind == "session":
        return rows
    return [name for name in rows if project_ok(ctx, name)]


def schedule_projects(task) -> tuple:
    """Проекты задания расписания: `steps[].project`, иначе поле `project` бота.

    Настоящее задание расписания не хранит проекта в верхнем поле: проект есть только
    у его шагов. Пустой ответ — владелец неизвестен; проектный токен такое задание не
    видит (fail closed), иначе чужие расписания снова стали бы общими.
    """
    if not isinstance(task, dict):
        return ()
    names = []
    for step in task.get("steps") or ():
        if not isinstance(step, dict):
            continue
        project = str(step.get("project") or "").strip()
        if project:
            names.append(project)
    if not names:
        project = str(task.get("project") or "").strip()
        if project:
            names.append(project)
    return tuple(dict.fromkeys(names))


def _all_visible(ctx, projects) -> bool:
    if ctx is None:
        return False
    if ctx.kind == "session":
        return True
    owners = [str(project) for project in projects or () if str(project or "").strip()]
    if not owners:
        return False
    return all(project_ok(ctx, owner) for owner in owners)


def schedule_visible(ctx, task) -> bool:
    """Виден ли проектному токену весь заказ расписания; без владельца — не виден."""
    return _all_visible(ctx, schedule_projects(task))


def schedule_run_visible(ctx, run) -> bool:
    """Виден ли токену прогон расписания: каждый пункт обязан назвать видимый проект.

    Видимый id задания сам по себе ничего не разрешает: пункты прогона могли уйти в
    чужой проект. Пустой или битый список пунктов тоже не разрешает прогон проектному
    токену — владельца нет, а верхнее поле `project` у настоящих прогонов отсутствует.
    """
    if not isinstance(run, dict):
        return bool(ctx is not None and ctx.kind == "session")
    items = run.get("items")
    if items:
        if not isinstance(items, (list, tuple)):
            return bool(ctx is not None and ctx.kind == "session")
        owners = []
        for item in items:
            if not isinstance(item, dict):
                return bool(ctx is not None and ctx.kind == "session")
            project = str(item.get("project") or "").strip()
            if not project:
                return bool(ctx is not None and ctx.kind == "session")
            owners.append(project)
        return _all_visible(ctx, owners)
    return bool(ctx is not None and ctx.kind == "session")


def endpoint_scope(method, path) -> str:
    """Право, нужное endpoint'у из allowlist; пусто — путь удалённому токену закрыт."""
    clean = urlparse(str(path or "")).path or "/"
    key = (str(method or "GET").upper(), clean)
    if key not in REMOTE_ALLOWED:
        return ""
    if clean in REMOTE_ADMIN_PATHS:
        return "admin"
    if clean in REMOTE_WRITE_PATHS:
        return "remote:write"
    if clean in REMOTE_READ_PATHS:
        return "remote:read"
    return ""                                   # подтверждение проверит свой инструмент


def remote_allowed(ctx, method, path) -> bool:
    """Точный allowlist: сессия не ограничена, remote — только свои пути."""
    if ctx is None:
        return False
    if ctx.kind == "session":
        return True
    if ctx.kind != "remote":
        return False
    clean = urlparse(str(path or "")).path or "/"
    return (str(method or "GET").upper(), clean) in REMOTE_ALLOWED


def _limit_spec(spec):
    """`5`, `(5, 60)`, `{'limit': 5, 'window': 60}` или `'5/min'` → (5, 60.0)."""
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return int(spec), 60.0
    if isinstance(spec, (list, tuple)) and len(spec) >= 2:
        return int(spec[0]), float(spec[1])
    if isinstance(spec, dict):
        limit = spec.get("limit", spec.get("max", spec.get("calls")))
        window = spec.get("window", spec.get("seconds", spec.get("per", 60)))
        if limit is not None:
            return int(limit), float(window)
    if isinstance(spec, str):
        text = spec.strip().lower().replace(" ", "")
        match = re.fullmatch(r"(\d+)/(min|minute|hour|h|sec|s|second|\d+)", text)
        if match:
            count = int(match.group(1))
            unit = match.group(2)
            window = {"min": 60, "minute": 60, "hour": 3600, "h": 3600,
                      "sec": 1, "s": 1, "second": 1}.get(unit)
            return count, float(window if window is not None else int(unit))
    raise ValueError(f"непонятный предел частоты: {spec!r}")


class RateLimiter:
    """Предел частоты в памяти процесса: `allow(key, cost=1) -> (bool, retry_after_s)`.

    `limits` — отображение `scope -> предел`; ключ вызова `(scope, token_id, ip, route)`.
    Потокобезопасен, окно скользящее. Неизвестный scope не ограничивается.
    """

    def __init__(self, limits):
        self._limits = {str(scope): _limit_spec(spec)
                        for scope, spec in dict(limits or {}).items()}
        self._hits: dict = {}
        self._lock = threading.Lock()

    def allow(self, key, cost: int = 1):
        scope = key[0] if isinstance(key, tuple) and key else key
        spec = self._limits.get(scope)
        if not spec:
            return True, 0
        limit, window = spec
        cost = max(1, int(cost))
        now = time.monotonic()
        with self._lock:
            queue = self._hits.setdefault(key, [])
            edge = now - window
            if queue and queue[0] <= edge:
                self._hits[key] = queue = [stamp for stamp in queue if stamp > edge]
            if len(queue) + cost <= limit:
                queue.extend([now] * cost)
                return True, 0
            retry = 1 if not queue else max(1, math.ceil(queue[0] + window - now))
            return False, retry
