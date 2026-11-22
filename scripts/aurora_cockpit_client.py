#!/usr/bin/env python3
"""aurora_cockpit_client.py — HTTP-клиент панели для `aurora-remote`.

Один клиент на процесс. В локальном режиме он читает `cockpit/.session.json` (порт и
сессионный токен панели), в удалённом — файл токена `0600`, который создаёт MCP-TOKENS.
Токен не попадает ни в argv, ни в журнал, ни в текст ошибки: заголовок собирается здесь
и никуда больше не передаётся.

Методы повторяют endpoint'ы из `CONTRACT.md` 9.1. Особые случаи контракта учтены:
`ping` отдаёт `{app, kit, pid, ready}`, `health` всегда шлёт `project`, `restart` шлёт
JSON `{}`, `diagnostics` возвращает сырые байты zip, `state(part="core")` — список
проектов. Ошибки панели приходят как `{error, detail, code}` и превращаются в
`CockpitError`; 403 с истёкшей сессией повторяется один раз с перечитанным файлом.
"""
from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8787"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

# Коды из CONTRACT.md 11. Незнакомый код панели не пропускаем как есть: он станет
# `bad_request`/`unavailable` по HTTP-статусу, чтобы MCP-клиент не получил произвольную
# строку вместо машинного токена.
KNOWN_CODES = {
    "bad_request", "invalid_argument", "invalid_project", "unauthorized", "scope",
    "project", "admin_network", "safe_mode", "session", "not_found", "conflict",
    "confirmation_required", "confirmation_expired", "confirmation_mismatch",
    "too_large", "bad_content_type", "rate_limited", "internal", "unavailable",
}


class CockpitError(Exception):
    """Отказ панели или сети. `message` уже без токена, URL и query."""

    def __init__(self, status: int, code: str, message: str, detail=None):
        self.status = int(status or 0)
        self.code = str(code or "internal")
        self.message = str(message or "")
        self.detail = detail if isinstance(detail, dict) else {}
        super().__init__(self.message)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


def _kit_root(kit=None):
    if kit:
        return os.path.abspath(os.fspath(kit))
    try:
        from aurora_common import kit_root
        root = kit_root()
        if root:
            return os.path.abspath(os.fspath(root))
    except Exception:  # noqa: BLE001 — клиент должен импортироваться и без кита
        pass
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _looks_loopback(base_url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(base_url).hostname or ""
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def _redact(text, token: str = "") -> str:
    """Текст ошибки без токена, URL и длинных путей к домашним папкам."""
    text = str(text or "")
    if token:
        text = text.replace(token, "[redacted]")
    text = re.sub(r"[Bb]earer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(r"https?://[^\s,;]+", "[url]", text)
    text = re.sub(r"(?<!\w)(?:/Users|/home|/var|/tmp)/[^\s,;]+", "[path]", text)
    return text[:300]


def _error_from_payload(status: int, payload) -> CockpitError:
    """Превратить тело панели в CockpitError, не доверяя незнакомому коду."""
    payload = payload if isinstance(payload, dict) else {}
    message = str(payload.get("error") or payload.get("message") or "")
    code = str(payload.get("code") or "")
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    low = message.lower()
    if code not in KNOWN_CODES:
        if "не найден" in low or "нет файла" in low or "архив прогона" in low:
            code = "not_found"
        elif "проект" in low and ("не выбран" in low or "неизвест" in low or "недопуст" in low):
            code = "invalid_project"
        elif "сесси" in low or "токен" in low:
            code = "session" if status == 403 else "unauthorized"
        elif status == 401:
            code = "unauthorized"
        elif status == 403:
            code = "scope"
        elif status == 404:
            code = "not_found"
        elif status == 409:
            code = "conflict"
        elif status == 413:
            code = "too_large"
        elif status == 415:
            code = "bad_content_type"
        elif status == 429:
            code = "rate_limited"
        elif status >= 500:
            code = "unavailable"
        else:
            code = "bad_request"
    if not message:
        message = "панель отклонила запрос"
    return CockpitError(status, code, message, detail)


class CockpitClient:
    """HTTP-клиент панели. Токен живёт только в памяти клиента и в заголовке запроса."""

    def __init__(self, base_url="", token="", token_file="", timeout=30, *, kit=None):
        self._timeout = float(timeout or 30)
        self._kit = _kit_root(kit)
        self._session_path = os.path.join(self._kit, "cockpit", ".session.json")
        self._token_file = os.path.expanduser(token_file) if token_file else ""
        self._base_url = str(base_url or "").rstrip("/")
        self._token = ""
        self._bearer = False
        self._from_session = False
        self._load_credentials(str(token or ""))

    # ------------------------------------------------------------------ учётные данные
    def _read_token_file(self, path: str) -> tuple:
        """(token, base_url) из файла токена. Поддерживает сырой токен и JSON."""
        try:
            st = os.stat(path)
        except OSError as e:
            raise CockpitError(500, "internal", "файл токена не прочитан") from e
        if (os.name != "nt" and hasattr(stat, "S_IMODE")
                and stat.S_IMODE(st.st_mode) & 0o077):
            raise CockpitError(500, "internal", "файл токена должен иметь права 0600")
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError as e:
            raise CockpitError(500, "internal", "файл токена не прочитан") from e
        if not text:
            raise CockpitError(401, "unauthorized", "файл токена пуст")
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError as e:
                raise CockpitError(500, "internal", "файл токена не разобран") from e
            token = str((data or {}).get("token") or (data or {}).get("secret") or "")
            base = str((data or {}).get("base_url") or (data or {}).get("url") or "")
            return token, base
        return text, ""

    def _read_session(self) -> tuple:
        """(token, base_url) из `cockpit/.session.json`; пусто, если файла нет."""
        try:
            with open(self._session_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return "", ""
        if not isinstance(data, dict):
            return "", ""
        token = str(data.get("token") or "")
        base = str(data.get("url") or "")
        if "?t=" in base:
            base, _, legacy = base.partition("?t=")
            token = token or legacy
        if not base:
            try:
                port = int(data.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if port:
                base = "http://127.0.0.1:%d/" % port
        return token, base.rstrip("/")

    def _load_credentials(self, explicit_token: str) -> None:
        if self._token_file:
            token, base = self._read_token_file(self._token_file)
            self._token = token
            self._base_url = (self._base_url or base).rstrip("/")
            self._bearer = True
        elif explicit_token:
            self._token = explicit_token
            # Без адреса это локальная панель: заголовок X-Aurora-Token. Bearer — только
            # когда адрес явно не петлевой, иначе локальный сессионный токен уехал бы
            # на чужой хост.
            self._bearer = bool(self._base_url) and not _looks_loopback(self._base_url)
        else:
            token, base = self._read_session()
            self._from_session = True
            self._token = token
            if not self._base_url and base:
                self._base_url = base
            self._bearer = False
        if not self._base_url:
            self._base_url = DEFAULT_BASE

    def reload_session(self) -> bool:
        """Перечитать файл сессии. → сменился ли токен (или адрес по умолчанию)."""
        if not self._from_session:
            return False
        token, base = self._read_session()
        changed = bool(token) and token != self._token
        if token:
            self._token = token
        if base and (not self._base_url or self._base_url == DEFAULT_BASE):
            self._base_url = base
            changed = True
        return changed

    def session_token(self) -> str:
        """Сессионный токен для локального stdio. Никуда не печатается."""
        return self._token

    def auth_token(self) -> str:
        """Действующий токен клиента для проверки реестром. Никуда не печатается."""
        return self._token

    # ------------------------------------------------------------------ транспорт
    def _url(self, path: str, query=None) -> str:
        url = self._base_url.rstrip("/") + "/" + str(path).lstrip("/")
        items = []
        for key, value in (query or {}).items():
            if value is None or value == "":
                continue
            if isinstance(value, (list, tuple)):
                for item in value:
                    items.append((key, str(item)))
            else:
                items.append((key, str(value)))
        if items:
            url += "?" + urllib.parse.urlencode(items)
        return url

    def _headers(self, body: bool) -> dict:
        headers = {"Accept": "application/json"}
        if self._token:
            if self._bearer:
                headers["Authorization"] = "Bearer " + self._token
            else:
                headers["X-Aurora-Token"] = self._token
        if body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method: str, path: str, query=None, body=None, *,
                 raw=False, text=False, retry=True):
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._url(path, query), data=data,
                                     headers=self._headers(data is not None), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                blob = response.read()
                ctype = (response.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as e:
            blob = e.read() if hasattr(e, "read") else b""
            payload = _json_or_empty(blob)
            error = _error_from_payload(int(e.code or 0), payload)
            if (error.code == "session" and retry and self._from_session
                    and self.reload_session()):
                return self._request(method, path, query=query, body=body,
                                     raw=raw, text=text, retry=False)
            error.message = _redact(error.message, self._token)
            raise error
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise CockpitError(503, "unavailable", "панель недоступна") from e
        if raw:
            return blob
        if text:
            return blob.decode("utf-8", "replace")
        if not blob:
            return {}
        if "json" not in ctype and ctype:
            raise CockpitError(500, "internal", "ответ панели не JSON")
        payload = _json_or_empty(blob)
        if payload is None:
            raise CockpitError(500, "internal", "ответ панели не разобран")
        if isinstance(payload, dict) and payload.get("error"):
            error = _error_from_payload(400, payload)
            error.message = _redact(error.message, self._token)
            raise error
        return payload

    # ------------------------------------------------------------------ чтение
    def ping(self) -> dict:
        return self._request("GET", "/api/ping")

    def live(self) -> dict:
        return self._request("GET", "/api/live")

    def jobs(self, project="") -> dict:
        return self._request("GET", "/api/jobs", {"project": project})

    def tasks(self, project="") -> dict:
        return self._request("GET", "/api/tasks", {"project": project})

    def threads(self) -> dict:
        return self._request("GET", "/api/threads")

    def state(self, part="core") -> dict:
        return self._request("GET", "/api/state", {"part": part or "core"})

    def history(self, project) -> dict:
        return self._request("GET", "/api/history", {"project": project})

    def history_run(self, project, run_id) -> dict:
        return self._request("GET", "/api/history/run", {"project": project, "id": run_id})

    def run_steps(self, project, run_id) -> dict:
        return self._request("GET", "/api/run/steps", {"project": project, "run": run_id})

    def run_logs(self, project) -> dict:
        return self._request("GET", "/api/run/logs", {"project": project})

    def run_file(self, project, run_id, stream="") -> dict:
        return self._request("GET", "/api/run/file",
                             {"project": project, "run": run_id, "stream": stream})

    def job(self, job_id, since=0) -> dict:
        return self._request("GET", "/api/job", {"id": job_id, "since": since})

    def route_live(self, route_id, since=0) -> dict:
        return self._request("GET", "/api/route/live", {"id": route_id, "since": since})

    def health(self, project, part="", fresh=False) -> dict:
        return self._request("GET", "/api/health", {"project": project, "part": part,
                                                    "fresh": "1" if fresh else ""})

    def agent(self) -> dict:
        return self._request("GET", "/api/agent")

    def net_state(self, project="") -> dict:
        return self._request("GET", "/api/net/state", {"project": project})

    def commands(self) -> dict:
        return self.state("extra")

    def scenarios(self, all=True) -> dict:
        return self._request("GET", "/api/scenarios", {"all": "1" if all else ""})

    def artifacts(self, project, kind="") -> dict:
        return self._request("GET", "/api/artifacts", {"project": project, "kind": kind})

    def report(self, project) -> dict:
        return self._request("GET", "/api/report", {"project": project})

    def report_file(self, project, report_id="analyst", stamp="") -> str:
        return self._request("GET", "/api/report/file",
                             {"project": project, "id": report_id, "stamp": stamp}, text=True)

    def kinds(self, project) -> dict:
        return self._request("GET", "/api/kinds", {"project": project})

    def cron(self) -> dict:
        return self._request("GET", "/api/cron")

    def mcp_kit(self) -> dict:
        return self._request("GET", "/api/mcp/kit")

    def run_errors(self, project, run_id, limit=50) -> dict:
        return self._request("GET", "/api/run/errors",
                             {"project": project, "run": run_id, "limit": limit})

    # ------------------------------------------------------------------ действия
    def task_check(self, project, cmd, args, kind="job") -> dict:
        return self._request("POST", "/api/task/check",
                             body={"project": project, "cmd": cmd,
                                   "args": list(args or []), "kind": kind})

    def run(self, project, cmd, args, resume=False, confirm_id="") -> dict:
        body = {"project": project, "cmd": cmd, "args": list(args or [])}
        if resume:
            body["resume"] = 1
        if confirm_id:
            body["confirm_id"] = confirm_id
        return self._request("POST", "/api/run", body=body)

    def job_stop(self, job_id) -> dict:
        return self._request("POST", "/api/job/stop", body={"id": job_id})

    def task_stop(self, task_id) -> dict:
        return self._request("POST", "/api/task/stop", body={"id": task_id})

    def route_run(self, project, route_id, write=True, resume=False, net="",
                  confirm_id="", extra=None) -> dict:
        body = {"project": project, "scId": route_id, "write": bool(write),
                "net": str(net or "")}
        if isinstance(extra, dict):
            for key, value in extra.items():
                body.setdefault(key, value)
        if resume:
            body["resume"] = 1
        if confirm_id:
            body["confirm_id"] = confirm_id
        return self._request("POST", "/api/route/run", body=body)

    def route_stop(self, route_id) -> dict:
        return self._request("POST", "/api/route/stop", body={"id": route_id})

    def ask_control(self, project, action, sid="", text="") -> dict:
        body = {"project": project, "action": action}
        if sid:
            body["sid"] = sid
        if text:
            body["text"] = text
        return self._request("POST", "/api/ask/control", body=body)

    def make_control(self, project, action, sid="", kind="") -> dict:
        body = {"project": project, "action": action}
        if sid:
            body["sid"] = sid
        if kind:
            body["kind"] = kind
        return self._request("POST", "/api/make/control", body=body)

    def cron_action(self, action, payload) -> dict:
        return self._request("POST", "/api/cron/" + str(action), body=dict(payload or {}))

    def diagnostics(self, project, run_id="", area="panel", deep=False) -> bytes:
        body = {"project": project, "area": area, "deep": bool(deep)}
        if run_id:
            body["run_id"] = run_id
        return self._request("POST", "/api/diagnostics", body=body, raw=True)

    def restart(self, confirm_id="") -> dict:
        body = {"confirm_id": confirm_id} if confirm_id else {}
        return self._request("POST", "/api/restart", body=body)

    def confirm_request(self, tool, project, args_digest, summary) -> dict:
        return self._request("POST", "/api/mcp/confirm/request",
                             body={"tool": tool, "project": project,
                                   "args_digest": args_digest, "summary": summary})

    # ------------------------------------------------------------------ вид
    def __repr__(self) -> str:
        return "CockpitClient(base_url=%r, remote=%s)" % (
            self._base_url, bool(self._token_file or self._bearer))


def _json_or_empty(blob):
    if not blob:
        return {}
    try:
        return json.loads(blob.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
