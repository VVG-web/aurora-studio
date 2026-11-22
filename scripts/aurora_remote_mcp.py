#!/usr/bin/env python3
"""aurora-remote: MCP-сервер управления панелью Авроры.

JSON-RPC 2.0 по stdio. Сервер не импортирует `aurora_mcp` и ничего не печатает в stdout,
кроме ответов протокола. Все 17 инструментов, их `inputSchema` и `outputSchema` взяты из
`mcp-schemas.json` контракта v2 (встроены сжатой копией, координационный файл в работе не
читается). `safe` — режим по умолчанию; опасные команды, destructive-маршруты и
необратимые действия закрыты, `full` добавляет их только с `admin` и подтверждением
человека. Токен приходит из файла `0600` или `cockpit/.session.json`, в argv и журналы не
попадает.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections import namedtuple
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from aurora_common import (
        MCP_SAFE_DENY_COMMANDS,
        confirmation_digest,
        kit_mcp_mode,
        mcp_command_verdict,
        mcp_route_verdict,
        utc_now,
    )
except ImportError:  # запуск из другого каталога: scripts/ лежит рядом с этим файлом
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from aurora_common import (
        MCP_SAFE_DENY_COMMANDS,
        confirmation_digest,
        kit_mcp_mode,
        mcp_command_verdict,
        mcp_route_verdict,
        utc_now,
    )

from aurora_cockpit_client import CockpitClient, CockpitError, _looks_loopback

# `aurora_guard.py` принадлежит MCP-TOKENS. Ветка MCP-REMOTE может собирать fake panel
# без неё; при импорте запоминаем причину, а `_ensure_guard()` скажет её при запуске.
try:
    from aurora_guard import (
        AuthContext,
        AuthError,
        RateLimiter,
        audit_write,
        project_ok,
        require_scope,
    )
    _GUARD_ERROR = None
except Exception as _guard_exc:  # noqa: BLE001 — модуль появится после слияния MCP-TOKENS
    AuthContext = None
    AuthError = Exception
    RateLimiter = None
    audit_write = None
    project_ok = None
    require_scope = None
    _GUARD_ERROR = _guard_exc

# Помощники расписания появились позже замороженных имён 10.7: их импорт не должен
# ронять удалённый сервер на ките со старым `aurora_guard`.
try:
    from aurora_guard import schedule_projects as _guard_schedule_projects
    from aurora_guard import schedule_run_visible as _guard_schedule_run_visible
    from aurora_guard import schedule_visible as _guard_schedule_visible
except Exception:  # noqa: BLE001 — остаются локальные правила ниже
    _guard_schedule_projects = None
    _guard_schedule_run_visible = None
    _guard_schedule_visible = None


# Сжатая копия `mcp-schemas.json` (sha256 файла 323500a7...). Не читаем координационный
# файл в работе: сервер должен работать из установленного кита без соседней папки.
_SCHEMAS_GZ_B64 = (
    "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"
)
SCHEMAS = json.loads(gzip.decompress(base64.b64decode(_SCHEMAS_GZ_B64)).decode("utf-8"))
TOOL_SCHEMAS = {item["name"]: item for item in SCHEMAS["tools"]}

READ_ONLY = (
    "remote_status", "remote_projects", "remote_runs", "remote_run", "remote_log",
    "remote_errors", "remote_diagnostics", "remote_commands", "remote_queue",
    "remote_results",
)
MANAGEMENT = (
    "remote_run_command", "remote_run_route", "remote_stop", "remote_cron",
    "remote_ask_control", "remote_make_control", "remote_restart",
)
if tuple(sorted(READ_ONLY + MANAGEMENT)) != tuple(sorted(TOOL_SCHEMAS)):
    raise RuntimeError("mcp-schemas.json не совпадает с 17 инструментами контракта")

TOOLS = [
    {
        "name": name,
        "description": TOOL_SCHEMAS[name].get("description", ""),
        "inputSchema": TOOL_SCHEMAS[name]["inputSchema"],
        "outputSchema": TOOL_SCHEMAS[name]["outputSchema"],
        "annotations": TOOL_SCHEMAS[name].get("annotations", {}),
    }
    for name in READ_ONLY + MANAGEMENT
]

ALL_SCOPES = frozenset({"knowledge:read", "remote:read", "remote:write", "admin"})
MCP_RATE_LIMITS = {
    "remote:read": (120, 60),
    "remote:write": (30, 60),
    "remote:run": (10, 60),
    "remote:stop": (10, 60),
    "remote:admin": (5, 60),
    "remote:confirm": (5, 60),
}
ModeInfo = namedtuple("ModeInfo", "mode server_mode mismatch panel_ok local_mode local_source")
ToolResult = namedtuple("ToolResult", "data next truncated audit")


class ToolError(Exception):
    def __init__(self, code, message, details=None, data=None):
        self.code = str(code or "internal")
        self.message = str(message or "")
        self.details = details if isinstance(details, dict) else {}
        self.data = data
        super().__init__(self.message)


# --------------------------------------------------------------------- схемы
def _type_ok(value, kind) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return True


def validate_schema(schema, value, path="$"):
    """Проверка входа по JSON Schema из контракта; лишние поля запрещены."""
    errors = []
    if not isinstance(schema, dict):
        return errors
    if "oneOf" in schema:
        base = {k: v for k, v in schema.items() if k != "oneOf"}
        errors.extend(validate_schema(base, value, path))
        matches = 0
        for sub in schema["oneOf"]:
            merged = dict(base)
            merged.update(sub if isinstance(sub, dict) else {})
            if not validate_schema(merged, value, path):
                matches += 1
        if matches != 1:
            errors.append("%s: нужно ровно одно из %s" % (
                path, ", ".join(str((s or {}).get("required")) for s in schema["oneOf"])))
        return errors
    if "anyOf" in schema:
        if not any(not validate_schema(sub, value, path) for sub in schema["anyOf"]):
            errors.append("%s: не подходит ни один вариант" % path)
        return errors
    if "const" in schema and value != schema["const"]:
        errors.append("%s: ожидалось %r" % (path, schema["const"]))
    kind = schema.get("type")
    if kind:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_type_ok(value, k) for k in kinds):
            errors.append("%s: неверный тип" % path)
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: недопустимое значение" % path)
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append("%s: короче %s" % (path, schema["minLength"]))
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append("%s: длиннее %s" % (path, schema["maxLength"]))
        pattern = schema.get("pattern")
        if pattern and not re.search(pattern, value):
            errors.append("%s: не подходит под шаблон" % path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("%s: меньше %s" % (path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("%s: больше %s" % (path, schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append("%s: меньше %s элементов" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append("%s: больше %s элементов" % (path, schema["maxItems"]))
        if schema.get("uniqueItems"):
            seen = []
            for item in value:
                if item in seen:
                    errors.append("%s: значения повторяются" % path)
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item_schema, item, "%s[%d]" % (path, index)))
    if isinstance(value, dict):
        for required in schema.get("required") or []:
            if required not in value:
                errors.append("%s.%s: обязательное поле" % (path, required))
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                errors.extend(validate_schema(props[key], item, "%s.%s" % (path, key)))
            elif additional is False:
                errors.append("%s.%s: неизвестное поле" % (path, key))
            elif isinstance(additional, dict):
                errors.extend(validate_schema(additional, item, "%s.%s" % (path, key)))
    return errors


def apply_defaults(schema, value):
    """Копия аргументов с `default` из схемы; вход не меняется."""
    if not isinstance(schema, dict) or not isinstance(value, dict):
        return value
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out = dict(value)
    for key, spec in props.items():
        if key not in out and isinstance(spec, dict) and "default" in spec:
            out[key] = json.loads(json.dumps(spec["default"]))
        elif key in out and isinstance(spec, dict):
            out[key] = apply_defaults(spec, out[key])
    return out


# --------------------------------------------------------------------- утилиты
def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mode(value) -> str:
    return value if value in ("safe", "full") else "safe"


def _path_like(value) -> bool:
    text = str(value or "")
    return (not text or text in (".", "..") or "/" in text or "\\" in text
            or ".." in text or text.startswith("~"))


def _run_summary(run_id, meta):
    meta = meta if isinstance(meta, dict) else {}
    return {
        "id": str(run_id or meta.get("id") or ""),
        "status": str(meta.get("status") or "unknown"),
        "started": str(meta.get("started") or ""),
        "finished": str(meta.get("finished") or ""),
        "seconds": meta.get("seconds") if isinstance(meta.get("seconds"), (int, float)) else None,
        "timing": meta.get("timing") if isinstance(meta.get("timing"), dict) else None,
    }


def _task_projects(task):
    """Проекты задания расписания: `steps[].project`, иначе поле `project` бота.

    Панель отдаёт настоящие задания без верхнего `project`: владельца дают шаги.
    Пустой ответ — владелец неизвестен, проектному токену такое задание не принадлежит.
    """
    if _guard_schedule_projects is not None:
        try:
            return tuple(_guard_schedule_projects(task))
        except Exception:                       # noqa: BLE001 — отказ безопаснее угадывания
            return ()
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


def _task_visible(ctx, task):
    """Виден ли проектному токену весь заказ расписания."""
    if _guard_schedule_visible is not None:
        try:
            return bool(_guard_schedule_visible(ctx, task))
        except Exception:                       # noqa: BLE001
            return False
    if ctx is None:
        return False
    if getattr(ctx, "kind", "") == "session":
        return True
    owners = _task_projects(task)
    return bool(owners) and project_ok is not None \
        and all(project_ok(ctx, owner) for owner in owners)


def _run_visible(ctx, run):
    """Прогон цепочки виден, только если все проекты его пунктов входят в права."""
    if not isinstance(run, dict):
        return False
    if _guard_schedule_run_visible is not None:
        try:
            return bool(_guard_schedule_run_visible(ctx, run))
        except Exception:                       # noqa: BLE001
            return False
    return False


def _cap_text(text, cap: int) -> str:
    raw = str(text or "").encode("utf-8")
    if len(raw) <= cap:
        return str(text or "")
    return raw[:cap].decode("utf-8", "ignore")


def _safe_rel(path) -> str:
    text = str(path or "").replace("\\", "/").lstrip("/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return ""
    return "/".join(parts)


def _log_slice(text, offset, limit, max_bytes):
    lines = str(text or "").splitlines()
    offset = max(0, int(offset or 0))
    limit = max(1, min(200, int(limit or 200)))
    max_bytes = max(1, min(32768, int(max_bytes or 32768)))
    if offset >= len(lines):
        return [], "", False
    out, used, consumed, truncated = [], 0, 0, False
    for line in lines[offset:]:
        if len(out) >= limit:
            truncated = True
            break
        raw = line.encode("utf-8")
        if used + len(raw) + 1 > max_bytes:
            if not out:
                part = raw[:max_bytes].decode("utf-8", "ignore")
                out.append(part)
                used = len(part.encode("utf-8"))
                consumed += 1
            truncated = True
            break
        out.append(line)
        used += len(raw) + 1
        consumed += 1
    next_cursor = str(offset + consumed) if (offset + consumed) < len(lines) else ""
    if next_cursor:
        truncated = True
    return out, next_cursor, truncated


def _zip_summary(blob: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith("summary.md")]
            if not names:
                return []
            names.sort(key=lambda n: (n.count("/"), n))
            text = archive.read(names[0]).decode("utf-8", "replace")
    except (OSError, ValueError, zipfile.BadZipFile):
        return []
    return [_cap_text(line, 500) for line in text.splitlines()[:20]]


def _guard_ready():
    return AuthContext is not None and project_ok is not None and require_scope is not None


def _ensure_guard():
    if not _guard_ready():
        raise RuntimeError(
            "scripts/aurora_guard.py не найден: сначала должен прийти слой MCP-TOKENS (%s)"
            % (_GUARD_ERROR,))


def _install_selftest_guard():
    """Подставной guard только для `--selftest`, пока MCP-TOKENS не слит.

    Это не реализация прав: все проекты и права разрешены, журнал не пишется. Боевой
    запуск без `aurora_guard.py` падает в `_ensure_guard`.
    """
    global AuthContext, AuthError, RateLimiter, audit_write, project_ok, require_scope, _GUARD_ERROR
    if _guard_ready():
        return

    class _AuthError(Exception):
        def __init__(self, status, code, message):
            self.status, self.code, self.message = status, code, message
            super().__init__(message)

    class _AuthContext:
        def __init__(self, kind="remote", token_id="", user="", scopes=frozenset(),
                     projects=(), client_ip="", vpn=False, mode="safe", source=""):
            self.kind = kind
            self.token_id = token_id
            self.user = user
            self.scopes = frozenset(scopes or ())
            self.projects = tuple(projects or ())
            self.client_ip = client_ip
            self.vpn = bool(vpn)
            self.mode = mode
            self.source = source

    class _RateLimiter:
        def __init__(self, limits=None):
            self._limits = limits or {}

        def allow(self, key, cost=1):
            return True, 0

    def _project_ok(ctx, project):
        return "*" in (ctx.projects or ()) or str(project or "") in (ctx.projects or ())

    def _require_scope(ctx, scope):
        if scope not in (ctx.scopes or ()):
            raise _AuthError(403, "scope", "нет права %s" % scope)

    def _audit_write(path, record):
        return None

    AuthContext = _AuthContext
    AuthError = _AuthError
    RateLimiter = _RateLimiter
    audit_write = _audit_write
    project_ok = _project_ok
    require_scope = _require_scope
    _GUARD_ERROR = None


def _make_rate_limiter():
    if RateLimiter is None:
        return None
    try:
        return RateLimiter(MCP_RATE_LIMITS)
    except Exception:  # noqa: BLE001 — общий ограничитель панели остаётся главным
        return None


# --------------------------------------------------------------------- сервер
class RemoteServer:
    def __init__(self, client, ctx, *, kit=None, commands=None, audit=None,
                 audit_path="", rate_limiter=None, clock=None, mode_override=""):
        _ensure_guard()
        self.client = client
        self.ctx = ctx
        self.kit = os.path.abspath(os.fspath(kit)) if kit else os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.commands = set(commands) if commands is not None else None
        self.audit = audit if audit is not None else audit_write
        self.audit_path = audit_path or self._default_audit_path()
        self.rate_limiter = rate_limiter if rate_limiter is not None else _make_rate_limiter()
        self.clock = clock or time
        self.mode_override = _mode(mode_override) if mode_override else ""
        self._project_paths = {}
        self._project_paths_at = 0.0
        self._mismatch_seen = set()

    # ------------------------------------------------------------------ вызов
    def call_tool(self, name, args):
        started = time.monotonic()
        name = str(name or "")
        args = args if isinstance(args, dict) else {}
        spec = TOOL_SCHEMAS.get(name)
        if spec is None:
            return self._envelope(name, args, None,
                                  ToolError("invalid_argument", "неизвестный инструмент"),
                                  started, None, spec)
        schema = spec.get("inputSchema") or {}
        errors = validate_schema(schema, args)
        if errors:
            return self._envelope(name, args, None,
                                  ToolError("invalid_argument",
                                            "аргументы не соответствуют схеме",
                                            {"fields": errors[:8]}),
                                  started, None, spec)
        args = apply_defaults(schema, args)
        project = args.get("project") if isinstance(args.get("project"), str) else ""
        info = None
        try:
            if project:
                self._check_project(project)
            base_scope = str(spec.get("scope") or "remote:read")
            self._require_scope(base_scope)
            info = self._effective_mode(project)
            self._rate_limit(name, base_scope)
            result = self._dispatch(name, args, info)
            envelope = self._envelope(name, args, result, None, started, info, spec)
            self._audit_call(name, args, info, envelope, None, result)
            return envelope
        except ToolError as error:
            envelope = self._envelope(name, args, error.data, error, started, info, spec)
            self._audit_call(name, args, info, envelope, error, None)
            return envelope
        except CockpitError as error:
            tool_error = ToolError(error.code, error.message, error.detail)
            envelope = self._envelope(name, args, None, tool_error, started, info, spec)
            self._audit_call(name, args, info, envelope, tool_error, None)
            return envelope
        except Exception as error:  # noqa: BLE001 — наружу уходит машинный код, не текст
            tool_error = ToolError("internal", "внутренняя ошибка инструмента")
            envelope = self._envelope(name, args, None, tool_error, started, info, spec)
            self._audit_call(name, args, info, envelope, tool_error, None)
            sys.stderr.write("aurora-remote: %s: %s\n" % (type(error).__name__, error))
            return envelope

    def _dispatch(self, name, args, info):
        return getattr(self, "_tool_" + name)(args, info)

    def _envelope(self, name, args, result, error, started, info, spec=None):
        spec = spec or TOOL_SCHEMAS.get(name) or {}
        props = (spec.get("inputSchema") or {}).get("properties") or {}
        data = None
        next_cursor = ""
        truncated = False
        if error is None and isinstance(result, ToolResult):
            data = result.data
            next_cursor = str(result.next or "")
            truncated = bool(result.truncated)
        elif error is not None and error.data is not None:
            data = error.data
        mode = info.mode if info else self._local_mode("")[0]
        envelope = {
            "ok": error is None,
            "tool": name,
            "data": data,
            "next": next_cursor,
            "meta": {
                "mode": mode,
                "duration_ms": max(0, int(round((self.clock.monotonic() - started) * 1000))),
                "truncated": truncated,
            },
        }
        if "project" in props:
            envelope["project"] = str(args.get("project") or "")
        if error is not None:
            body = {"code": error.code, "message": error.message}
            if error.details:
                body["details"] = error.details
            envelope["error"] = body
        return envelope

    # ------------------------------------------------------------------ режим
    def _local_mode(self, project):
        if self.mode_override:
            return self.mode_override, "debug"
        try:
            mode, source = kit_mcp_mode(project, kit=self.kit)
        except Exception:  # noqa: BLE001 — битый файл не должен разрешать опасное
            return "safe", "broken"
        return _mode(mode), source

    def _effective_mode(self, project):
        local_mode, local_source = self._local_mode(project)
        server_mode = ""
        panel_ok = False
        try:
            payload = self.client.mcp_kit()
            panel_ok = True
            remote = payload.get("remote") if isinstance(payload, dict) else None
            if isinstance(remote, dict):
                mode = remote.get("mode")
                server_mode = mode if mode in ("safe", "full") else ""
                overrides = remote.get("projects")
                if project and overrides is not None:
                    if not isinstance(overrides, dict):
                        server_mode = "safe"
                    elif project in overrides:
                        override = overrides.get(project)
                        server_mode = override if override in ("safe", "full") else "safe"
        except CockpitError:
            panel_ok = False
        if not server_mode:
            if panel_ok:
                # Панель ответила, но режим не назвала: неизвестность не даёт full.
                mismatch = local_mode != "safe"
                if mismatch:
                    self._audit_mode_mismatch(project, local_mode, "safe", local_source)
                return ModeInfo("safe", "", mismatch, panel_ok, local_mode, local_source)
            return ModeInfo(local_mode, local_mode, False, panel_ok, local_mode, local_source)
        effective = "safe" if "safe" in (local_mode, server_mode) else "full"
        mismatch = local_mode != server_mode
        if mismatch:
            self._audit_mode_mismatch(project, local_mode, server_mode, local_source)
        return ModeInfo(effective, server_mode, mismatch, panel_ok,
                        local_mode, local_source)

    def _audit_mode_mismatch(self, project, local_mode, server_mode, source):
        key = (str(project or ""), local_mode, server_mode)
        if key in self._mismatch_seen:
            return
        self._mismatch_seen.add(key)
        self._write_audit({
            "at": _utc_now(), "tool": "remote_status", "mode": local_mode,
            "project": str(project or ""), "cmd": "", "route": "", "args_digest": "",
            "job": "", "result": "mode_mismatch", "error": "mode_mismatch",
            "actor": getattr(self.ctx, "user", ""), "client": getattr(self.ctx, "client_ip", ""),
            "token_id": getattr(self.ctx, "token_id", ""), "scope": "", "vpn": bool(
                getattr(self.ctx, "vpn", False)), "confirm_id": "",
            "local_mode": local_mode, "server_mode": server_mode, "source": source,
        })

    # ------------------------------------------------------------------ права
    def _check_project(self, project):
        if _path_like(project):
            raise ToolError("invalid_project", "недопустимый проект")
        try:
            allowed = bool(project_ok(self.ctx, project))
        except Exception:  # noqa: BLE001 — отказ ACL не должен пропускать дальше
            allowed = False
        if not allowed:
            raise ToolError("project", "проект не разрешён токену")

    def _require_scope(self, scope):
        try:
            require_scope(self.ctx, scope)
        except AuthError as error:
            raise ToolError(getattr(error, "code", "scope") or "scope",
                            getattr(error, "message", "нет права") or "нет права")
        except Exception:  # noqa: BLE001
            raise ToolError("scope", "нет права %s" % scope)

    def _visible_projects(self):
        projects = tuple(getattr(self.ctx, "projects", ()) or ())
        if "*" in projects:
            return None
        return [str(p) for p in projects if p]

    def _rate_limit(self, tool, scope):
        if self.rate_limiter is None:
            return
        key = (scope, getattr(self.ctx, "token_id", "") or "session",
               getattr(self.ctx, "client_ip", ""), tool)
        try:
            allowed, retry_after = self.rate_limiter.allow(key, 1)
        except Exception:  # noqa: BLE001 — ограничитель панели всё равно сработает
            return
        if not allowed:
            raise ToolError("rate_limited", "слишком много вызовов",
                            {"retry_after": max(1, int(retry_after or 1))})

    def _project_path(self, project):
        now = self.clock.monotonic()
        if self._project_paths and now - self._project_paths_at < 30:
            path = self._project_paths.get(project)
            if path:
                return path
        state = self.client.state("core")
        projects = state.get("projects") if isinstance(state, dict) else None
        mapping = {}
        for item in projects or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            path = str(item.get("path") or "")
            if not path:
                continue
            if name:
                mapping[name] = path
            mapping.setdefault(os.path.basename(path.rstrip("/\\")), path)
        self._project_paths = mapping
        self._project_paths_at = now
        path = mapping.get(project)
        if not path:
            raise ToolError("invalid_project", "проект не найден в панели")
        return path

    # ------------------------------------------------------------------ команды
    def _command_rows(self):
        rows = []
        try:
            payload = self.client.commands()
            raw = payload.get("commands") if isinstance(payload, dict) else None
            if isinstance(raw, list):
                for row in raw:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("cmd") or row.get("name") or "")
                    if not name:
                        continue
                    rows.append({
                        "name": name,
                        "group": str(row.get("ns") or row.get("group") or ""),
                        "summary": str(row.get("what") or row.get("summary") or ""),
                        "runnable": bool(row.get("runnable", True)),
                    })
        except CockpitError:
            rows = []
        if rows:
            return rows
        path = os.path.join(self.kit, "commands.txt")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not parts[1]:
                continue
            executor = parts[3] if len(parts) > 3 else ""
            rows.append({
                "name": parts[1],
                "group": parts[0],
                "summary": parts[6] if len(parts) > 6 else "",
                "runnable": "скрипт" in executor,
            })
        return rows

    def _command_names(self):
        if self.commands is not None:
            return set(self.commands)
        names = set()
        path = os.path.join(self.kit, "commands.txt")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return names
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 2 or not parts[1]:
                continue
            names.add(parts[1])
            if len(parts) > 2 and parts[2] not in ("", "—", "-"):
                for alias in parts[2].split(","):
                    if alias.strip():
                        names.add(alias.strip())
        return names

    # ------------------------------------------------------------------ audit
    def _default_audit_path(self):
        stamp = utc_now().strftime("%Y%m")
        return os.path.join(self.kit, "local", "audit", "aurora-remote-%s.jsonl" % stamp)

    def _write_audit(self, record):
        if not callable(self.audit):
            return
        safe = {}
        for key, value in record.items():
            if key in ("body", "args", "token", "secret", "prompt", "headers"):
                continue
            safe[key] = value
        try:
            self.audit(self.audit_path, safe)
        except Exception:  # noqa: BLE001 — журнал не меняет результат действия
            pass

    def _audit_call(self, name, args, info, envelope, error, result):
        spec = TOOL_SCHEMAS.get(name) or {}
        audit = result.audit if isinstance(result, ToolResult) and isinstance(result.audit, dict) else {}
        job = audit.get("job") or ""
        if not job and isinstance(envelope.get("data"), dict):
            job = envelope["data"].get("job") or ""
        self._write_audit({
            "at": _utc_now(),
            "tool": name,
            "mode": envelope["meta"]["mode"],
            "project": envelope.get("project", ""),
            "cmd": audit.get("cmd") or (args.get("cmd") if name == "remote_run_command" else ""),
            "route": audit.get("route") or (args.get("route") if name == "remote_run_route" else ""),
            "args_digest": audit.get("args_digest") or "",
            "job": job if isinstance(job, str) else "",
            "result": "ok" if envelope["ok"] else "error",
            "error": "" if envelope["ok"] else envelope["error"]["code"],
            "actor": getattr(self.ctx, "user", ""),
            "client": getattr(self.ctx, "client_ip", ""),
            "token_id": getattr(self.ctx, "token_id", ""),
            "scope": str(spec.get("scope") or ""),
            "vpn": bool(getattr(self.ctx, "vpn", False)),
            "confirm_id": str(args.get("confirm_id") or audit.get("confirm_id") or ""),
        })

    # ------------------------------------------------------------------ инструменты
    def _tool_remote_status(self, args, info):
        ping = self.client.ping()
        if not isinstance(ping, dict):
            raise CockpitError(503, "unavailable", "панель ответила не объектом")
        data = {
            "app": str(ping.get("app") or ""),
            "kit": str(ping.get("kit") or ""),
            "pid": int(ping.get("pid") or 0),
            "ready": bool(ping.get("ready")),
            "mode": info.mode,
            "remote_mode": info.server_mode or info.mode,
            "panel_ok": True,
        }
        return ToolResult(data, "", False, {})

    def _tool_remote_projects(self, args, info):
        state = self.client.state("core")
        mapping = {}
        projects = []
        for item in (state.get("projects") if isinstance(state, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            path = str(item.get("path") or "")
            if path:
                mapping[name or os.path.basename(path.rstrip("/\\"))] = path
            if not name:
                continue
            try:
                allowed = bool(project_ok(self.ctx, name))
            except Exception:  # noqa: BLE001
                allowed = False
            if allowed:
                projects.append({"name": name,
                                 "activity": item.get("activity")
                                 if isinstance(item.get("activity"), dict) else {}})
        if mapping:
            self._project_paths = mapping
            self._project_paths_at = self.clock.monotonic()
        return ToolResult({"projects": projects[:500]}, "", len(projects) > 500, {})

    def _tool_remote_runs(self, args, info):
        path = self._project_path(args["project"])
        payload = self.client.history(path)
        runs = payload.get("runs") if isinstance(payload, dict) else None
        runs = [r for r in (runs or []) if isinstance(r, dict)]
        since = str(args.get("since") or "")
        if since:
            ids = [str(r.get("id") or "") for r in runs]
            if since in ids:
                runs = runs[ids.index(since) + 1:]
        status = str(args.get("status") or "")
        if status:
            runs = [r for r in runs if str(r.get("status") or "unknown") == status]
        limit = int(args.get("limit") or 50)
        window = runs[:limit]
        next_id = ""
        if len(runs) > limit and window:
            next_id = str(runs[limit].get("id") or "")
        data = {"runs": [_run_summary(r.get("id"), r) for r in window]}
        return ToolResult(data, next_id, len(runs) > limit, {})

    def _tool_remote_run(self, args, info):
        path = self._project_path(args["project"])
        payload = self.client.history_run(path, args["run_id"])
        if not isinstance(payload, dict) or payload.get("error"):
            raise ToolError("not_found", "прогон не найден")
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
        return ToolResult({"run": _run_summary(args["run_id"], meta),
                           "entries": entries}, "", False, {})

    def _tool_remote_log(self, args, info):
        if _path_like(args["run_id"]):
            raise ToolError("invalid_argument", "недопустимый run_id")
        path = self._project_path(args["project"])
        payload = self.client.run_file(path, args["run_id"], str(args.get("stream") or "run"))
        if not isinstance(payload, dict) or payload.get("error"):
            raise ToolError("not_found", "прогон не найден")
        if isinstance(payload.get("text"), str):
            text = payload["text"]
        elif isinstance(payload.get("entries"), list):
            lines = []
            for item in payload["entries"]:
                if isinstance(item, dict):
                    lines.append(str(item.get("s") or item.get("text") or ""))
                else:
                    lines.append(str(item))
            text = "\n".join(lines)
        else:
            raise ToolError("not_found", "журнал прогона не найден")
        lines, next_cursor, truncated = _log_slice(
            text, args.get("offset") or 0, args.get("limit") or 200,
            args.get("max_bytes") or 32768)
        return ToolResult({"lines": lines, "truncated": truncated},
                          next_cursor, truncated, {})

    def _tool_remote_errors(self, args, info):
        if _path_like(args["run_id"]):
            raise ToolError("invalid_argument", "недопустимый run_id")
        path = self._project_path(args["project"])
        payload = self.client.run_errors(path, args["run_id"], args.get("limit") or 50)
        if not isinstance(payload, dict) or payload.get("error"):
            raise ToolError("not_found", "прогон не найден")
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        clean = []
        for item in errors[:100]:
            if not isinstance(item, dict):
                continue
            clean.append({
                "at": str(item.get("at") or ""),
                "kind": str(item.get("kind") or "failure"),
                "message": _cap_text(item.get("message") or "", 500),
            })
        return ToolResult({"errors": clean, "count": len(clean)}, "", False, {})

    def _tool_remote_diagnostics(self, args, info):
        path = self._project_path(args["project"])
        blob = self.client.diagnostics(path, args.get("run_id") or "",
                                       args.get("area") or "panel", bool(args.get("deep")))
        if not isinstance(blob, (bytes, bytearray)) or not blob:
            raise ToolError("internal", "панель вернула пустой архив")
        data = self._save_diagnostics(bytes(blob))
        return ToolResult(data, "", False, {})

    def _save_diagnostics(self, blob):
        now = utc_now()
        folder = os.path.join(os.path.expanduser("~"), ".jcode", "scratch",
                              "aurora-diag-%s" % now.strftime("%Y%m%d"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "aurora-diag-%s-%d.zip" % (
            now.strftime("%Y%m%d-%H%M%S"), os.getpid()))
        with open(path, "wb") as f:
            f.write(blob)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {"path": path, "bytes": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
                "summary_lines": _zip_summary(blob)}

    def _tool_remote_commands(self, args, info):
        group = str(args.get("group") or "")
        rows = self._command_rows()
        if group:
            rows = [r for r in rows if r["group"] == group]
        rows.sort(key=lambda r: r["name"])
        return ToolResult({"commands": rows[:500]}, "", len(rows) > 500, {})

    def _tool_remote_queue(self, args, info):
        visible = self._visible_projects()
        projects = visible if visible is not None else [""]
        jobs, tasks = [], []
        for project in projects:
            path = self._project_path(project) if project else ""
            payload = self.client.jobs(path)
            jobs.extend([j for j in (payload.get("jobs") if isinstance(payload, dict) else None) or []
                         if isinstance(j, dict)])
            snapshot = self.client.tasks(path)
            if isinstance(snapshot, dict):
                for key in ("active", "queued"):
                    tasks.extend([t for t in snapshot.get(key) or [] if isinstance(t, dict)])
                if isinstance(snapshot.get("tasks"), list):
                    tasks.extend([t for t in snapshot["tasks"] if isinstance(t, dict)])
        threads_payload = self.client.threads()
        threads = []
        if isinstance(threads_payload, dict):
            raw = threads_payload.get("rows")
            if not isinstance(raw, list):
                raw = threads_payload.get("threads")
            for row in raw or []:
                if not isinstance(row, dict):
                    continue
                owner = str(row.get("project") or "")
                if visible is not None and owner:
                    allowed = set(visible)
                    allowed.update(self._project_paths.get(p, "") for p in visible)
                    if owner not in allowed:
                        continue
                threads.append(row)
        return ToolResult({"jobs": jobs[:200], "tasks": tasks[:200], "threads": threads[:200]},
                          "", max(len(jobs), len(tasks), len(threads)) > 200, {})

    def _tool_remote_results(self, args, info):
        include = args.get("include") or ["run", "history", "artifacts", "report"]
        path = self._project_path(args["project"])
        run_id = str(args.get("run_id") or "")
        history_payload = self.client.history(path)
        runs = history_payload.get("runs") if isinstance(history_payload, dict) else None
        runs = [r for r in (runs or []) if isinstance(r, dict)]
        row = None
        if run_id:
            row = next((r for r in runs if str(r.get("id") or "") == run_id), None)
        elif runs:
            row = runs[0]
            run_id = str(row.get("id") or "")
        data, truncated = {}, False
        history_data = None
        if "history" in include and run_id:
            try:
                payload = self.client.history_run(path, run_id)
            except CockpitError:
                payload = None
            if isinstance(payload, dict) and not payload.get("error"):
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
                history_data = {"run": _run_summary(run_id, meta), "entries": entries, "meta": meta}
        summary = _run_summary(run_id, row) if row else (
            history_data["run"] if history_data else None)
        for key in include:
            if key == "run":
                data["run"] = summary
            elif key == "history":
                data["history"] = history_data
            elif key == "artifacts":
                data["artifacts"], capped = self._collect_artifacts(path)
                truncated = truncated or capped
            elif key == "report":
                data["report"] = self._collect_report(path)
            elif key == "kinds":
                data["kinds"] = self._collect_kinds(path)
        return ToolResult(data, "", truncated, {})

    def _collect_artifacts(self, path):
        kinds = self._collect_kinds(path)
        if not kinds:
            payload = self.client.artifacts(path)
            files = payload.get("files") if isinstance(payload, dict) else None
            return self._map_artifacts(files, ""), False
        out = []
        for kind in kinds[:50]:
            try:
                payload = self.client.artifacts(path, kind)
            except CockpitError:
                continue
            files = payload.get("files") if isinstance(payload, dict) else None
            out.extend(self._map_artifacts(files, kind))
            if len(out) >= 200:
                return out[:200], True
        return out, False

    @staticmethod
    def _map_artifacts(files, kind):
        out = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            rel = _safe_rel(item.get("rel") or item.get("path") or item.get("name") or "")
            if not rel:
                continue
            out.append({
                "path": rel,
                "kind": str(item.get("kind") or kind or ""),
                "size": int(item.get("size") or 0),
                "mtime": float(item.get("mtime") or 0),
            })
        return out

    def _collect_report(self, path):
        try:
            state = self.client.report(path)
        except CockpitError:
            return None
        if isinstance(state, dict) and isinstance(state.get("text"), str):
            return {"path": str(state.get("path") or ""),
                    "text": _cap_text(state["text"], 65536)}
        reports = state.get("reports") if isinstance(state, dict) else None
        for item in reports or []:
            if not isinstance(item, dict):
                continue
            output = item.get("output") if isinstance(item.get("output"), dict) else {}
            if not (item.get("exists") or output.get("exists")):
                continue
            report_id = str(item.get("id") or "analyst")
            rel = _safe_rel(output.get("path") or item.get("path") or "")
            try:
                text = self.client.report_file(path, report_id, str(item.get("stamp") or ""))
            except CockpitError:
                return None
            return {"path": rel, "text": _cap_text(text, 65536)}
        return None

    def _collect_kinds(self, path):
        try:
            payload = self.client.kinds(path)
        except CockpitError:
            return None
        if not isinstance(payload, dict):
            return None
        kinds = payload.get("kinds")
        if isinstance(kinds, dict):
            return sorted(str(k) for k in kinds)
        if isinstance(kinds, list):
            return sorted(str(k) for k in kinds)
        return None

    def _tool_remote_run_command(self, args, info):
        cmd = str(args["cmd"])
        if cmd not in self._command_names():
            raise ToolError("invalid_argument", "команда не найдена в реестре")
        verdict = mcp_command_verdict(cmd, args.get("args"), info.mode, self.ctx.scopes)
        if not verdict["allowed"]:
            raise ToolError(verdict["reason"] or "scope", "команда запрещена в этом режиме")
        confirm_id = str(args.get("confirm_id") or "")
        cmd_args = list(args.get("args") or [])
        resume = bool(args.get("resume"))
        action_args = {"cmd": cmd, "args": cmd_args, "resume": resume}
        path = self._project_path(args["project"])
        check = self.client.task_check(path, cmd, cmd_args, "job")
        if isinstance(check, dict) and check.get("can_start") is False:
            raise ToolError("conflict", "проект занят другой задачей",
                            {"conflict": check.get("conflict"), "can_start": False},
                            {"job": None, "conflict": True, "can_start": False})
        if resume and not self._resume_ok(args["project"], path, cmd, cmd_args, route=False):
            raise ToolError("not_found", "нет прогона для продолжения")
        if verdict["confirm"]:
            self._require_scope("admin")
            if not confirm_id:
                self._raise_confirmation("remote_run_command", args["project"], action_args,
                                         "запуск %s в %s" % (cmd, args["project"]))
        payload = self.client.run(path, cmd, cmd_args,
                                  resume=resume,
                                  confirm_id=confirm_id if verdict["confirm"] else "")
        job = payload.get("job") if isinstance(payload, dict) else None
        return ToolResult({"job": job, "conflict": False, "can_start": True},
                          "", False, {"cmd": cmd, "job": job})

    def _tool_remote_run_route(self, args, info):
        route_id = str(args["route"])
        scenarios = self.client.scenarios(True)
        items = scenarios.get("scenarios") if isinstance(scenarios, dict) else None
        route = next((r for r in (items or []) if isinstance(r, dict)
                      and str(r.get("id") or r.get("scId") or "") == route_id), None)
        if route is None:
            raise ToolError("not_found", "маршрут не найден")
        verdict = mcp_route_verdict(route, info.mode)
        if not verdict["allowed"]:
            raise ToolError(verdict["reason"] or "safe_mode", "маршрут запрещён в этом режиме")
        extra = args.get("args") if isinstance(args.get("args"), dict) else {}
        write = bool(extra.get("write", route.get("write", True)))
        net = str(extra.get("net") or "")
        resume = bool(args.get("resume"))
        # `args` — открытый объект. Служебные поля запроса из него не берём: иначе
        # `args.confirm_id` подменил бы подтверждение человека, а `project`/`scId`
        # увели бы запуск в другой маршрут.
        reserved = ("write", "net", "resume", "confirm_id", "project", "scId")
        extra_body = {k: v for k, v in extra.items() if k not in reserved}
        action_args = {"scId": route_id, "write": write, "net": net, "resume": resume}
        action_args.update(extra_body)
        confirm_id = str(args.get("confirm_id") or "")
        path = self._project_path(args["project"])
        if resume and not self._resume_ok(args["project"], path, route_id, route=True):
            raise ToolError("not_found", "нет маршрута для продолжения")
        if verdict["confirm"]:
            self._require_scope("admin")
            if not confirm_id:
                self._raise_confirmation("remote_run_route", args["project"], action_args,
                                         "маршрут %s в %s" % (route_id, args["project"]))
        payload = self.client.route_run(path, route_id, write=write,
                                        resume=resume, net=net,
                                        confirm_id=confirm_id if verdict["confirm"] else "",
                                        extra=extra_body)
        if isinstance(payload, dict) and payload.get("net_blocked"):
            raise ToolError("conflict", "маршрут заблокирован проверкой связи",
                            {"net_blocked": payload["net_blocked"]},
                            {"route": route_id, "net_blocked": payload["net_blocked"]})
        result = payload.get("route") if isinstance(payload, dict) else route_id
        return ToolResult({"route": result or route_id, "net_blocked": None},
                          "", False, {"route": route_id})

    def _resume_ok(self, project, path, identity, argv=(), route=False):
        payload = self.client.history(path)
        rows = payload.get("runs") if isinstance(payload, dict) else None
        expected = "" if route else (identity + " " + " ".join(argv)).strip()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") not in ("stopped", "failed", "done"):
                continue
            if route:
                if str(row.get("kind") or "") != "route":
                    continue
                if str(row.get("scId") or "") != identity:
                    continue
                return True
            else:
                kind = str(row.get("kind") or "")
                if kind and kind != "command":
                    continue
                title = str(row.get("title") or "").strip()
                if title:
                    if title == expected:
                        return True
                    continue
                # Старая запись без title: история не отдала argv, сверяем хотя бы команду.
                if str(row.get("cmd") or "") == identity:
                    return True
        return False

    def _tool_remote_stop(self, args, info):
        path = self._project_path(args["project"])
        if args.get("job_id"):
            jobs = self.client.jobs(path).get("jobs") or []
            if not any(isinstance(row, dict) and str(row.get("id") or "") == args["job_id"]
                       for row in jobs):
                raise ToolError("not_found", "задание не найдено в этом проекте")
            payload = self.client.job_stop(args["job_id"])
            stopped = "job"
        elif args.get("task_id"):
            snapshot = self.client.tasks(path)
            ids = []
            for key in ("active", "queued"):
                ids.extend(str(row.get("id") or "") for row in snapshot.get(key) or []
                           if isinstance(row, dict))
            if args["task_id"] not in ids:
                raise ToolError("not_found", "задача не найдена в этом проекте")
            payload = self.client.task_stop(args["task_id"])
            stopped = "task"
        else:
            live = self.client.live()
            routes = live.get("routes") if isinstance(live, dict) else None
            route = next((row for row in routes or []
                          if isinstance(row, dict)
                          and str(row.get("id") or "") == args["route_id"]), None)
            if route is None:
                raise ToolError("not_found", "маршрут не найден")
            owner = str(route.get("project") or "")
            if owner and os.path.normpath(owner) != os.path.normpath(path):
                raise ToolError("project", "маршрут принадлежит другому проекту")
            payload = self.client.route_stop(args["route_id"])
            stopped = "route"
        if isinstance(payload, dict) and payload.get("error"):
            raise ToolError("not_found", "останавливать уже нечего")
        ok = bool(payload.get("ok", True)) if isinstance(payload, dict) else True
        return ToolResult({"ok": ok, "stopped": stopped}, "", False, {})

    def _cron_task_projects(self, job):
        """Проекты задания расписания по его id; пусто, если задание не найдено."""
        if not job:
            return []
        try:
            payload = self.client.cron()
        except CockpitError:
            return []
        for row in (payload.get("tasks") if isinstance(payload, dict) else None) or []:
            if isinstance(row, dict) and str(row.get("id") or "") == job:
                return list(_task_projects(row))
        bots = payload.get("bots") if isinstance(payload, dict) else None
        if isinstance(bots, dict):
            for project, rows in bots.items():
                for row in rows or []:
                    if isinstance(row, dict) and str(row.get("id") or "") == job:
                        return [str(project)]
        return []

    @staticmethod
    def _cron_owner_ok(owners, path, slug="") -> bool:
        """Все проекты задания — проект этого вызова; пустой список не проходит."""
        if not owners:
            return False
        want = os.path.normpath(str(path))
        for owner in owners:
            text = str(owner or "")
            if slug and text == str(slug):
                continue
            if os.path.normpath(text) != want:
                return False
        return True

    def _cron_step_ok(self, step, path, slug) -> bool:
        """Шаг сохраняемого задания: проект входит в права токена и в проект вызова."""
        step_project = str(step.get("project") or "").strip()
        if not step_project:
            return True
        if step_project == "*":
            return False
        try:
            if not project_ok(self.ctx, step_project):
                return False
        except Exception:                       # noqa: BLE001 — отказ безопаснее угадывания
            return False
        return self._cron_owner_ok([step_project], path, slug)

    def _tool_remote_cron(self, args, info):
        action = str(args["action"])
        path = self._project_path(args["project"])
        job = str(args.get("job") or "")
        spec = args.get("spec") if isinstance(args.get("spec"), dict) else {}
        task_id = job or (str(spec.get("id") or "") if action == "save" else "")
        owners = self._cron_task_projects(task_id) if task_id else []
        if task_id.startswith("bot-") and not owners:
            raise ToolError("not_found", "бот не найден в этом проекте")
        if task_id and not owners:
            raise ToolError("not_found", "задание расписания не найдено")
        if task_id and not self._cron_owner_ok(owners, path, args["project"]):
            raise ToolError("project", "задание расписания принадлежит другому проекту")
        if action == "save":
            for step in spec.get("steps") or []:
                if isinstance(step, dict) and not self._cron_step_ok(step, path,
                                                                     args["project"]):
                    raise ToolError("project", "шаг расписания в другом проекте")
        if action == "stop":
            state = self.client.cron()
            current = state.get("current") if isinstance(state, dict) else None
            if isinstance(current, dict):
                current_id = str(current.get("task") or "")
                current_owners = self._cron_task_projects(current_id) if current_id else []
                if not self._cron_owner_ok(current_owners, path, args["project"]):
                    raise ToolError("project", "идёт цепочка другого проекта")
        confirm_id = str(args.get("confirm_id") or "")
        payload = {"project": path, "action": action}
        if action == "save":
            task = dict(spec)
            if job:
                task["id"] = job
            payload["task"] = task
        elif action == "toggle":
            payload["id"] = job
            payload["enabled"] = bool(spec.get("enabled", True))
        elif action != "stop":
            payload["id"] = job
        if action in ("save", "delete"):
            if info.mode == "safe":
                raise ToolError("safe_mode", "правка расписания запрещена в safe")
            self._require_scope("admin")
            if not confirm_id:
                action_args = {k: v for k, v in payload.items() if k != "project"}
                self._raise_confirmation("remote_cron", args["project"],
                                         action_args,
                                         "правка расписания в %s" % args["project"])
        if action in ("save", "delete"):
            payload["confirm_id"] = confirm_id
        result = self.client.cron_action(action, payload)
        if isinstance(result, dict) and result.get("ok") is False:
            code = str(result.get("error") or "")
            if code in ("no_task", "not_found"):
                raise ToolError("not_found", "задание расписания не найдено")
            raise ToolError("invalid_argument", "расписание не приняло действие")
        return ToolResult({"ok": True, "cron": self._filtered_cron(self.client.cron())},
                          "", False, {"cmd": "cron:" + action})

    def _filtered_cron(self, payload):
        if not isinstance(payload, dict):
            return None
        visible = self._visible_projects()
        if visible is None:
            return payload
        allowed_names = set(visible)
        allowed_paths = set()
        out = dict(payload)
        projects = []
        for item in payload.get("projects") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            path = str(item.get("path") or "")
            if name in allowed_names or path in allowed_paths or os.path.basename(
                    path.rstrip("/\\")) in allowed_names:
                projects.append(item)
                if path:
                    allowed_paths.add(path)
        out["projects"] = projects
        tasks = [row for row in out.get("tasks") or [] if _task_visible(self.ctx, row)]
        out["tasks"] = tasks
        visible_ids = {str(row.get("id") or "") for row in tasks if isinstance(row, dict)}
        current = out.get("current")
        if current is not None and not _run_visible(self.ctx, current):
            out["current"] = None
        if isinstance(out.get("queue"), list):
            out["queue"] = [tid for tid in out["queue"] if str(tid) in visible_ids]
        for key in ("runs", "items"):
            if isinstance(out.get(key), list):
                out[key] = [row for row in out[key]
                            if _run_visible(self.ctx, row)]
        if isinstance(out.get("bots"), dict):
            out["bots"] = {key: value for key, value in out["bots"].items()
                           if os.path.basename(str(key).rstrip("/\\")) in allowed_names
                           or str(key) in allowed_paths}
        return out

    def _tool_remote_ask_control(self, args, info):
        path = self._project_path(args["project"])
        payload = self.client.ask_control(path, args["action"], str(args.get("sid") or ""),
                                          str(args.get("text") or ""))
        return self._control_result(payload, args)

    def _tool_remote_make_control(self, args, info):
        path = self._project_path(args["project"])
        payload = self.client.make_control(path, args["action"], str(args.get("sid") or ""),
                                           str(args.get("kind") or ""))
        return self._control_result(payload, args)

    def _control_result(self, payload, args):
        if isinstance(payload, dict) and payload.get("error"):
            raise ToolError("bad_request", "действие не принято")
        return ToolResult({"ok": True, "sid": self._control_sid(payload, args),
                           "action": args["action"]}, "", False, {})

    @staticmethod
    def _control_sid(payload, args):
        """Панель может не вернуть sid (старый ответ); тогда остаётся запрошенный."""
        if isinstance(payload, dict) and payload.get("sid") is not None:
            return payload["sid"]
        return args.get("sid")

    def _tool_remote_restart(self, args, info):
        if info.mode == "safe":
            raise ToolError("safe_mode", "перезапуск панели запрещён в safe")
        self._require_scope("admin")
        confirm_id = str(args.get("confirm_id") or "")
        if not confirm_id:
            self._raise_confirmation("remote_restart", "", {}, "перезапуск панели")
        payload = self.client.restart(confirm_id)
        if isinstance(payload, dict) and payload.get("error"):
            raise ToolError("unavailable", "панель не перезапустилась")
        return ToolResult({"ok": True, "restarting": True}, "", False, {})

    def _raise_confirmation(self, tool, project, action_args, summary):
        digest = confirmation_digest(tool, project, action_args)
        payload = self.client.confirm_request(tool, project, digest, summary)
        request_id = payload.get("request_id") if isinstance(payload, dict) else ""
        expires_at = payload.get("expires_at") if isinstance(payload, dict) else ""
        if not request_id:
            raise ToolError("unavailable", "панель не выдала заявку на подтверждение")
        raise ToolError("confirmation_required", "нужно подтверждение человека",
                        {"request_id": str(request_id), "expires_at": str(expires_at),
                         "args_digest": str(payload.get("args_digest") or digest)})


# --------------------------------------------------------------------- stdio
def _write_response(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _jsonrpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve_stdio(server):
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write_response(_jsonrpc_error(None, -32700, "parse error"))
            continue
        requests = message if isinstance(message, list) else [message]
        responses = []
        for request in requests:
            if not isinstance(request, dict):
                responses.append(_jsonrpc_error(None, -32600, "invalid request"))
                continue
            method = request.get("method")
            msg_id = request.get("id")
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            if method == "initialize":
                responses.append({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "aurora-remote", "version": "1.0.0"},
                }})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                responses.append({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                envelope = server.call_tool(name, arguments)
                responses.append({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(
                        envelope, ensure_ascii=False, separators=(",", ":"))}],
                    "structuredContent": envelope,
                    "isError": not envelope.get("ok", False),
                }})
            elif method == "ping":
                responses.append({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            elif method == "shutdown":
                responses.append({"jsonrpc": "2.0", "id": msg_id, "result": None})
            elif method == "exit":
                return
            else:
                responses.append(_jsonrpc_error(msg_id, -32601, "method not found"))
        if not responses:
            continue
        _write_response(responses if isinstance(message, list) else responses[0])


# --------------------------------------------------------------------- selftest
class _SelftestPanel:
    def __init__(self, token):
        self.token = token
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send(self, code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self):
                got = self.headers.get("X-Aurora-Token") or ""
                if not got:
                    auth = self.headers.get("Authorization") or ""
                    if auth.startswith("Bearer "):
                        got = auth[len("Bearer "):]
                if got != outer.token:
                    self._send(403, {"error": "сессия", "code": "session"})
                    return False
                return True

            def do_GET(self):
                if not self._auth():
                    return
                if self.path.split("?")[0] == "/api/ping":
                    self._send(200, {"app": "aurora-cockpit", "kit": "selftest",
                                     "pid": 1, "ready": True})
                    return
                if self.path.split("?")[0] == "/api/mcp/kit":
                    self._send(200, {"remote": {"mode": "safe", "stored": "safe",
                                                "projects": {}, "source": "file",
                                                "deny": list(MCP_SAFE_DENY_COMMANDS),
                                                "admin_network": False}})
                    return
                self._send(404, {"error": "неизвестный маршрут", "code": "not_found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def run_selftest():
    _install_selftest_guard()
    kit = tempfile.mkdtemp(prefix="aurora-remote-selftest-")
    token = "selftest-token"
    panel = _SelftestPanel(token)
    try:
        os.makedirs(os.path.join(kit, "local"), exist_ok=True)
        with open(os.path.join(kit, "local", "mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"auroraRemote": {"mode": "safe"}}, f)
        client = CockpitClient(base_url=panel.url, token=token, kit=kit)
        ctx = AuthContext(kind="session", scopes=ALL_SCOPES, projects=("*",), source="session")
        server = RemoteServer(client, ctx, kit=kit,
                              audit_path=os.path.join(kit, "local", "audit", "selftest.jsonl"))
        envelope = server.call_tool("remote_status", {})
        ok = (len(TOOLS) == 17 and envelope.get("ok") is True
              and envelope.get("data", {}).get("mode") in ("safe", "full"))
        print(json.dumps({"tools": len(TOOLS), "status_ok": bool(envelope.get("ok")),
                          "mode": envelope.get("data", {}).get("mode"),
                          "selftest": "ok" if ok else "failed"},
                         ensure_ascii=False, separators=(",", ":")))
        return 0 if ok else 1
    finally:
        panel.close()
        shutil.rmtree(kit, ignore_errors=True)


# --------------------------------------------------------------------- CLI
def _auth_from_token_file(token_file, kit):
    client = CockpitClient(token_file=token_file, kit=kit)
    token = client.auth_token()
    if not token:
        raise SystemExit("файл токена пуст")
    try:
        import mcp_tokens
    except ImportError as error:
        raise SystemExit("scripts/mcp_tokens.py не найден: нужен слой MCP-TOKENS") from error
    record = mcp_tokens.verify(token, kit=kit)
    if not isinstance(record, dict):
        raise SystemExit("токен не принят")
    if str(record.get("kind") or "") != "remote":
        raise SystemExit("нужен токен вида remote")
    return client, AuthContext(
        kind="remote", token_id=str(record.get("id") or ""),
        user=str(record.get("user") or ""),
        scopes=frozenset(record.get("scopes") or ()),
        projects=tuple(str(p) for p in (record.get("projects") or ())),
        client_ip="", vpn=False, source="header")


def main(argv=None):
    parser = argparse.ArgumentParser(description="aurora-remote MCP server (stdio)")
    parser.add_argument("--all", action="store_true",
                        help="локальная сессия панели со всеми проектами")
    parser.add_argument("--url", default="", help="адрес панели для удалённого режима")
    parser.add_argument("--token-file", default="", help="файл токена 0600")
    parser.add_argument("--selftest", action="store_true",
                        help="проверить список инструментов на подставной панели")
    parser.add_argument("--mode", choices=("safe", "full"), default="",
                        help="только для --selftest и отладки")
    args = parser.parse_args(argv)
    if args.selftest:
        return run_selftest()
    if args.mode:
        sys.stderr.write("aurora-remote: --mode только для отладки; режим панели главнее\n")
    _ensure_guard()
    kit = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    if args.token_file:
        if args.all:
            raise SystemExit("--all несовместим с удалённым токеном")
        client, ctx = _auth_from_token_file(args.token_file, kit)
        if args.url:
            client = CockpitClient(base_url=args.url, token_file=args.token_file, kit=kit)
    else:
        if args.url and not _looks_loopback(args.url):
            raise SystemExit("удалённый адрес требует --token-file: сессионный токен "
                             "панели на чужой хост не отправляется")
        client = CockpitClient(base_url=args.url, kit=kit)
        if not client.auth_token():
            raise SystemExit("нет токена сессии: запустите панель или укажите --token-file")
        ctx = AuthContext(kind="session", token_id="", user=os.environ.get("USER", ""),
                          scopes=ALL_SCOPES, projects=("*",), source="session")
    server = RemoteServer(client, ctx, kit=kit, mode_override=args.mode)
    serve_stdio(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
