#!/usr/bin/env python3
"""mcp_tokens.py — реестр токенов MCP Авроры.

Один файл кита `<kit>/local/mcp-tokens.json`, права `0600`, версия 1. В файле лежат
только `sha256(secret)` и префикс: сам секрет показывается один раз при выпуске и
больше нигде не хранится. Сравнение — `hmac.compare_digest`.

Токен: `aur_kn_<id>_<secret>` (знания) или `aur_rm_<id>_<secret>` (удалённое
управление). `projects: []` означает «ни одного проекта», а не «все». Звёздочка в
реестре запрещена: локальный администратор — это сессия панели, а не запись токена.

Все функции принимают keyword-only `kit`/`path`; `path` главнее `kit`, а
`AURORA_MCP_TOKENS_FILE` — запасной путь для тестов. При `AURORA_TESTS_ISOLATED=1`
запись в настоящий реестр кита запрещена: тест обязан передать временный кит или путь.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

try:                                            # движок ищет кит сам; в тестах его может не быть
    import aurora_common as AC
except Exception:                               # noqa: BLE001 — реестр не должен падать из-за движка
    AC = None

TOKEN_VERSION = 1
SCOPES = ("knowledge:read", "remote:read", "remote:write", "admin")
TOKEN_PREFIX_KN = "aur_kn_"
TOKEN_PREFIX_RM = "aur_rm_"
MAX_ACTIVE_PER_KIND = 20
MAX_ACTIVE_PER_KIT = 100
DEFAULT_EXPIRES_DAYS = 90
MAX_EXPIRES_DAYS = 365
LAST_USED_STEP_S = 60

KINDS = ("knowledge", "remote")
_PREFIX = {"knowledge": TOKEN_PREFIX_KN, "remote": TOKEN_PREFIX_RM}
_FILE_NAME = "mcp-tokens.json"
_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_LEN = 6
_DUMMY_HASH = "sha256:" + hashlib.sha256(b"aurora-mcp-token-dummy").hexdigest()

# Один замок на процесс: чтение-изменение-запись реестра не должны перемешиваться между
# потоками панели. Замок общий для всех путей — файлов в работе единицы.
_LOCK = threading.RLock()


class TokenError(Exception):
    """Отказ реестра с кодом контракта: `{error, code}` на HTTP-слое."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


class TokenRegistryError(TokenError):
    """Файл реестра повреждён. Удалённые токены при этом не принимаются."""

    def __init__(self, message: str):
        super().__init__(500, "internal", message)


def _env_path() -> str:
    return (os.environ.get("AURORA_MCP_TOKENS_FILE") or "").strip()


def _real_kit_root() -> str:
    if AC is not None:
        try:
            root = AC.kit_root()
            if root:
                return os.path.realpath(str(root))
        except Exception:                       # noqa: BLE001, S110 — путь кита не должен ронять реестр
            pass
    return os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _real_kit_file() -> str:
    return os.path.join(_real_kit_root(), "local", _FILE_NAME)


def _expand(value) -> str:
    return os.path.realpath(os.path.expanduser(os.fspath(value)))


def _resolve(kit=None, path="") -> str:
    """Путь реестра: явный путь > кит > тестовый env > настоящий кит."""
    if path:
        return _expand(path)
    if kit:
        return os.path.join(_expand(kit), "local", _FILE_NAME)
    env = _env_path()
    if env:
        return _expand(env)
    return os.path.realpath(_real_kit_file())


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _check_isolated(path: str) -> None:
    """Тест не имеет права читать или писать личный реестр машины."""
    if os.environ.get("AURORA_TESTS_ISOLATED") and _same_path(path, _real_kit_file()):
        raise RuntimeError("refusing real token registry in isolated tests")


def _now() -> float:
    return time.time()


def _utc(ts: float | None = None) -> str:
    return datetime.fromtimestamp(_now() if ts is None else ts, timezone.utc) \
        .replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ") \
            .replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _hash_secret(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _empty() -> dict:
    return {"version": TOKEN_VERSION, "tokens": []}


def _valid_project(project) -> str:
    if not isinstance(project, str):
        raise TokenError(400, "invalid_project", "проект токена должен быть строкой")
    value = project.strip()
    if (not value or value == "*" or value in (".", "..")
            or "/" in value or "\\" in value or "\x00" in value
            or any(ord(ch) < 32 for ch in value)):
        raise TokenError(400, "invalid_project",
                         "проект токена — явное имя без '*', '/' и путей")
    return value


def _norm_projects(projects) -> list[str]:
    if projects is None:
        return []
    if not isinstance(projects, (list, tuple)):
        raise TokenError(400, "invalid_project", "projects должен быть списком проектов")
    out: list[str] = []
    for item in projects:
        value = _valid_project(item)
        if value not in out:
            out.append(value)
    return out


def _norm_scopes(scopes) -> list[str]:
    if not isinstance(scopes, (list, tuple)) or not scopes:
        raise TokenError(400, "bad_request", "scopes должен быть непустым списком прав")
    out: list[str] = []
    for item in scopes:
        if not isinstance(item, str) or not item:
            raise TokenError(400, "bad_request", "право токена должно быть строкой")
        if item not in SCOPES:
            raise TokenError(400, "bad_request", f"неизвестное право токена: {item}")
        if item not in out:
            out.append(item)
    return out


def _expires_days(value) -> int:
    if value is None:
        return DEFAULT_EXPIRES_DAYS
    if isinstance(value, bool):
        raise TokenError(400, "bad_request", "expires_days должен быть числом дней")
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise TokenError(400, "bad_request", "expires_days должен быть числом дней") from None
    if days < 1 or days > MAX_EXPIRES_DAYS:
        raise TokenError(400, "bad_request",
                         f"expires_days вне диапазона 1..{MAX_EXPIRES_DAYS}")
    return days


def _clean_name(name) -> str:
    value = str(name or "").strip()
    if not value or len(value) > 200 or any(ord(ch) < 32 for ch in value):
        raise TokenError(400, "bad_request", "имя токена — непустая строка до 200 знаков")
    return value


def _valid_record(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    kind = record.get("kind")
    if kind not in KINDS:
        return False
    if not isinstance(record.get("id"), str) or not record["id"]:
        return False
    if not re.fullmatch(r"[a-z0-9]{1,32}", record["id"]):
        return False
    if not isinstance(record.get("prefix"), str) \
            or not record["prefix"].startswith(_PREFIX[kind]):
        return False
    if record["prefix"] != _PREFIX[kind] + record["id"] + "_":
        return False
    if not isinstance(record.get("hash"), str) or not record["hash"].startswith("sha256:"):
        return False
    if _parse_ts(record.get("expires_at")) is None:
        return False
    for key in ("scopes", "projects"):
        if not isinstance(record.get(key), list):
            return False
    if any(scope not in SCOPES for scope in record["scopes"]):
        return False
    return not any(not isinstance(project, str) or not project or project == "*"
                   or "/" in project or "\\" in project for project in record["projects"])


def _validate(data) -> dict:
    if not isinstance(data, dict) or data.get("version") != TOKEN_VERSION:
        raise TokenRegistryError("local/mcp-tokens.json: нет версии 1")
    tokens = data.get("tokens")
    if not isinstance(tokens, list):
        raise TokenRegistryError("local/mcp-tokens.json: tokens должен быть списком")
    seen: set[str] = set()
    for record in tokens:
        if not _valid_record(record):
            raise TokenRegistryError("local/mcp-tokens.json: запись токена повреждена")
        if record["id"] in seen:
            raise TokenRegistryError("local/mcp-tokens.json: повтор id токена")
        seen.add(record["id"])
    return data


def _read(path: str) -> dict:
    if not os.path.exists(path):
        return _empty()
    try:
        if os.path.getsize(path) > 4 * 1024 * 1024:
            raise TokenRegistryError("local/mcp-tokens.json: файл слишком велик")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except TokenError:
        raise
    except (OSError, ValueError) as e:
        raise TokenRegistryError(f"local/mcp-tokens.json не разобран: {type(e).__name__}") from None
    return _validate(data)


def _write_private(path: str, blob: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob)
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write(path: str, data: dict) -> None:
    blob = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                old = f.read()
            _write_private(path + ".bak", old)
        except OSError:
            pass
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(3)}"
    try:
        _write_private(tmp, blob)
        if AC is not None and hasattr(AC, "replace_file"):
            AC.replace_file(tmp, path)
        else:
            os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _active(record: dict, now: float | None = None) -> bool:
    if record.get("revoked_at"):
        return False
    expires = _parse_ts(record.get("expires_at"))
    return not (expires is not None and expires <= (_now() if now is None else now))


def _status(record: dict) -> str:
    if record.get("revoked_at"):
        return "revoked"
    expires = _parse_ts(record.get("expires_at"))
    if expires is not None and expires <= _now():
        return "expired"
    return "active"


def _public_record(record: dict) -> dict:
    """Запись без хэша: хэш нужен только файлу реестра."""
    return {key: value for key, value in record.items() if key != "hash"}


def _list_record(record: dict) -> dict:
    return {
        "id": record.get("id", ""),
        "name": record.get("name", ""),
        "prefix": record.get("prefix", ""),
        "kind": record.get("kind", ""),
        "scopes": list(record.get("scopes") or []),
        "projects": list(record.get("projects") or []),
        "created_at": record.get("created_at", ""),
        "last_used_at": record.get("last_used_at", ""),
        "last_used_ip": record.get("last_used_ip", ""),
        "expires_at": record.get("expires_at", ""),
        "status": _status(record),
    }


def _find(data: dict, token_id: str) -> dict | None:
    wanted = str(token_id or "")
    if not wanted:
        return None
    return next((record for record in data["tokens"] if record.get("id") == wanted), None)


def _new_id(data: dict) -> str:
    used = {record.get("id") for record in data["tokens"]}
    for _ in range(100):
        value = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LEN))
        if value not in used:
            return value
    raise TokenError(500, "internal", "не удалось подобрать id токена")


def load_registry(*, kit=None, path="") -> dict:
    """Прочитать реестр. Нет файла — пустой реестр версии 1."""
    target = _resolve(kit, path)
    _check_isolated(target)
    with _LOCK:
        return _read(target)


def save_registry(data: dict, *, kit=None, path="") -> str:
    """Записать реестр целиком: атомарно, `0600`, прежний файл — в `.bak`."""
    normalized = _validate(data)
    target = _resolve(kit, path)
    _check_isolated(target)
    with _LOCK:
        _write(target, normalized)
    return target


def registry_problem(*, kit=None, path="") -> str:
    """Почему реестр не читается (пусто — всё в порядке). Панель показывает причину."""
    try:
        load_registry(kit=kit, path=path)
        return ""
    except (TokenRegistryError, RuntimeError, OSError) as e:
        return str(e)
    except TokenError as e:
        return e.message


def scopes_for_kind(kind: str) -> set:
    """Какие права допустимы для вида токена."""
    if kind == "knowledge":
        return {"knowledge:read"}
    if kind == "remote":
        return {"remote:read", "remote:write", "admin"}
    raise TokenError(400, "bad_request", f"неизвестный вид токена: {kind}")


def check_project(record: dict, project: str) -> bool:
    """Проект входит в ACL записи. Пустой проект и `*` не проходят никогда."""
    if not isinstance(record, dict):
        return False
    wanted = str(project or "").strip()
    if not wanted or wanted == "*":
        return False
    projects = record.get("projects") or []
    if wanted in projects:
        return True
    # Панель зовёт проект путём, а токен хранит короткое имя: сравниваем и хвост пути.
    base = os.path.basename(wanted.rstrip("/\\"))
    return bool(base) and base in projects


def new_token(kind, name, user, scopes, projects, expires_days, *, kit=None, path=""):
    """Выпустить токен. → `{token, record}`; секрет возвращается один раз."""
    kind = str(kind or "")
    allowed = scopes_for_kind(kind)
    clean_name = _clean_name(name)
    clean_scopes = _norm_scopes(scopes)
    bad = [scope for scope in clean_scopes if scope not in allowed]
    if bad:
        raise TokenError(400, "bad_request",
                         f"права {', '.join(bad)} не подходят токену вида {kind}")
    clean_projects = _norm_projects(projects)
    days = _expires_days(expires_days)
    target = _resolve(kit, path)
    _check_isolated(target)
    with _LOCK:
        data = _read(target)
        active = [record for record in data["tokens"] if _active(record)]
        if sum(1 for record in active if record.get("kind") == kind) >= MAX_ACTIVE_PER_KIND:
            raise TokenError(409, "conflict",
                             f"уже {MAX_ACTIVE_PER_KIND} активных токенов вида {kind}")
        if len(active) >= MAX_ACTIVE_PER_KIT:
            raise TokenError(409, "conflict",
                             f"уже {MAX_ACTIVE_PER_KIT} активных токенов в ките")
        token_id = _new_id(data)
        prefix = _PREFIX[kind] + token_id + "_"
        secret = secrets.token_urlsafe(32)
        created = _now()
        record = {
            "id": token_id,
            "kind": kind,
            "name": clean_name,
            "user": str(user or "")[:200],
            "prefix": prefix,
            "hash": _hash_secret(secret),
            "scopes": clean_scopes,
            "projects": clean_projects,
            "created_at": _utc(created),
            "created_by": "panel",
            "last_used_at": "",
            "last_used_ip": "",
            "expires_at": _utc(created + days * 86400),
            "revoked_at": None,
            "rotated_at": None,
            "rotated_count": 0,
        }
        data["tokens"].append(record)
        _write(target, data)
    return {"token": prefix + secret, "record": _public_record(record)}


def verify(token, *, kit=None, path=""):
    """Проверить токен. → запись или None. Битая запись реестра — None (fail closed)."""
    raw = str(token or "")
    kind = ""
    rest = ""
    for name, prefix in _PREFIX.items():
        if raw.startswith(prefix):
            kind, rest = name, raw[len(prefix):]
            break
    if not kind:
        return None
    token_id, sep, secret = rest.partition("_")
    if not sep or not token_id or not secret:
        return None
    target = _resolve(kit, path)
    try:
        _check_isolated(target)
        with _LOCK:
            data = _read(target)
    except (TokenError, RuntimeError, OSError):
        return None
    record = _find(data, token_id)
    expected = record.get("hash") if record and record.get("kind") == kind else _DUMMY_HASH
    digest = _hash_secret(secret)
    if not hmac.compare_digest(digest.encode("utf-8"), str(expected).encode("utf-8")):
        return None
    if not record or not _active(record):
        return None
    return dict(record)


def rotate(token_id, *, kit=None, path=""):
    """Сменить секрет одной записи. Старый секрет перестаёт работать сразу."""
    target = _resolve(kit, path)
    _check_isolated(target)
    with _LOCK:
        data = _read(target)
        record = _find(data, token_id)
        if not record:
            raise TokenError(404, "not_found", "токен не найден")
        if record.get("revoked_at"):
            raise TokenError(409, "conflict", "отозванный токен не вращают — выпустите новый")
        if not _active(record):
            raise TokenError(409, "conflict", "срок токена истёк — выпустите новый")
        kind = record.get("kind", "")
        prefix = _PREFIX.get(kind)
        if not prefix:
            raise TokenRegistryError("запись токена повреждена: неизвестный вид")
        secret = secrets.token_urlsafe(32)
        record["prefix"] = prefix + str(record.get("id") or "") + "_"
        record["hash"] = _hash_secret(secret)
        record["rotated_at"] = _utc()
        record["rotated_count"] = int(record.get("rotated_count") or 0) + 1
        _write(target, data)
    return {"token": record["prefix"] + secret, "record": _public_record(record)}


def revoke(token_id, *, kit=None, path=""):
    """Отозвать токен. Запись остаётся, `revoked_at` ставится один раз."""
    target = _resolve(kit, path)
    _check_isolated(target)
    with _LOCK:
        data = _read(target)
        record = _find(data, token_id)
        if not record:
            raise TokenError(404, "not_found", "токен не найден")
        if not record.get("revoked_at"):
            record["revoked_at"] = _utc()
            _write(target, data)
    return {"ok": True, "record": _public_record(record)}


def rename(token_id, name, *, kit=None, path=""):
    """Переименовать запись. Секрет и права не меняются."""
    clean_name = _clean_name(name)
    target = _resolve(kit, path)
    _check_isolated(target)
    with _LOCK:
        data = _read(target)
        record = _find(data, token_id)
        if not record:
            raise TokenError(404, "not_found", "токен не найден")
        record["name"] = clean_name
        _write(target, data)
    return {"ok": True, "record": _public_record(record)}


def list_tokens(kind=None, *, kit=None, path=""):
    """Список без секрета и хэша. `kind` — необязательный фильтр."""
    if kind not in (None, "", "knowledge", "remote"):
        raise TokenError(400, "bad_request", f"неизвестный вид токена: {kind}")
    data = load_registry(kit=kit, path=path)
    rows = data["tokens"]
    if kind:
        rows = [record for record in rows if record.get("kind") == kind]
    return [_list_record(record) for record in rows]


def last_used(token_id, *, kit=None, path=""):
    """Когда и с какого адреса токен применяли в последний раз."""
    data = load_registry(kit=kit, path=path)
    record = _find(data, token_id)
    if not record:
        raise TokenError(404, "not_found", "токен не найден")
    return {"id": record.get("id", ""),
            "last_used_at": record.get("last_used_at", ""),
            "last_used_ip": record.get("last_used_ip", "")}


def touch(token_id, ip="", *, kit=None, path=""):
    """Отметить применение токена, но не чаще раза в 60 секунд на запись."""
    target = _resolve(kit, path)
    _check_isolated(target)
    with _LOCK:
        data = _read(target)
        record = _find(data, token_id)
        if not record or not _active(record):
            return None
        now = _now()
        previous = _parse_ts(record.get("last_used_at"))
        if previous is not None and now - previous < LAST_USED_STEP_S:
            return dict(record)
        record["last_used_at"] = _utc(now)
        record["last_used_ip"] = str(ip or "")[:64]
        _write(target, data)
    return dict(record)
