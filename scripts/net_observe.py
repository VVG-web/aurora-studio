#!/usr/bin/env python3
"""net_observe.py — снимки сети, VPN и модельных провайдеров вокруг прогонов.

Зачем: «шаг шёл три часа» само по себе ничего не объясняет. Нужно различить три
разные вещи, которые раньше назывались одним словом «связь»:

- доступность профиля проверки (probe): отвечает ли внутренний адрес;
- состояние системного VPN: подключён ли туннель и какие сервисы активны;
- доступность конкретного провайдера LLM: жив ли его бэкенд.

Снимок делается на старте и конце каждой операции, а для операции дольше 12 минут —
ещё и каждые 12 минут. Подробный журнал лежит в архиве прогона и в git не едет.
Краткий итог прогона пишется в проект отдельным файлом участника и машины:
`AuroraKnowledgeDB/meta/net-runs.d/<actor>-<machine>/<run_id>.json`. Два участника
никогда не пишут в один файл, поэтому merge не конфликтует.

Снимок не содержит адресов probe, ключей, URL и путей. Имена системных VPN пишутся
намеренно: по ним видно, какой именно провайдер моделей остался без связи.
"""
from __future__ import annotations

import aurora_common  # noqa: F401 — защита версии Python

import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

for _s in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from aurora_actor import actor  # noqa: E402
from aurora_fs import write_json  # noqa: E402

LOCAL_NAME = "net.json"
SHARED_DIR = ("AuroraKnowledgeDB", "meta", "net-runs.d")
LONG_AFTER = 12 * 60                  # первая отметка долгой операции
LONG_EVERY = 12 * 60                  # дальше — с этим шагом
MAX_LONG = 48                         # больше суток долгих отметок не копим
MAX_SNAPSHOTS = MAX_LONG + 4
KEEP_RECORDS = 200                    # снимков на свой шард: больше не копим
KEEP_DAYS = 90                        # старше этого срока снимок не нужен
SYSTEM_TIMEOUT = 5
RUN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_LOCK = threading.RLock()
_OBSERVER = None


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(),
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_seconds(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def long_after() -> int:
    """Порог долгой операции. Переменная нужна тестам, а не человеку."""
    return _env_seconds("AURORA_NET_LONG_S", LONG_AFTER)


def long_every() -> int:
    return _env_seconds("AURORA_NET_LONG_EVERY_S", LONG_EVERY)


def enabled() -> bool:
    """Снимки — часть рабочего прогона, а не каждого теста: тесты не должны ждать сеть."""
    raw = os.environ.get("AURORA_NET_OBSERVE")
    if raw is not None:
        return raw != "0"
    return not os.environ.get("AURORA_TESTS_ISOLATED")


def machine_id() -> str:
    """Короткий стабильный идентификатор машины без имени хоста в пути."""
    forced = os.environ.get("AURORA_MACHINE", "").strip()
    if forced:
        raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", forced).strip("-")[:48] or "machine"
    else:
        raw = platform.node() or socket.gethostname() or "machine"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def local_path(run_dir: str | os.PathLike) -> Path:
    return Path(run_dir) / LOCAL_NAME


def shared_path(project: str, run_id: str) -> Path | None:
    """Файл участника и машины. Уникальный путь — вместо общего журнала с merge-конфликтами."""
    if not project or not RUN_RE.fullmatch(str(run_id or "")):
        return None
    try:
        actor_id, _display = actor(project)
    except Exception:  # noqa: BLE001 — идентичность не должна ломать снимок
        return None
    root = Path(project).expanduser().resolve()
    if not (root / "AuroraKnowledgeDB").is_dir():
        return None                 # движковые команды кита не должны заводить проектную папку
    folder = root.joinpath(*SHARED_DIR) / f"{actor_id}-{machine_id()}"
    return folder / f"{run_id}.json"


def _read(path: str | os.PathLike) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: str | os.PathLike, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(str(path), data, indent=1)


def _clean_name(value, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _project_name(project: str) -> str:
    """В снимке — имя проекта, а не путь: общий файл не должен выдавать домашний каталог."""
    return os.path.basename(os.path.normpath(str(project or ""))) or ""


def _requirements(project: str, cmd: str = "", steps: list | None = None) -> dict:
    """Требования команды или маршрута без запуска проверки связи."""
    try:
        import conn_gate as GATE
        if steps:
            rows = [GATE.command_requirements(c, project) for c in steps if c]
            req = GATE.merge_requirements(rows) if rows else {}
        elif cmd:
            req = GATE.command_requirements(cmd, project)
        else:
            req = {}
        profiles = sorted(set(req.get("profiles") or []) | set(req.get("llm_profiles") or []))
        providers = []
        if req.get("llm"):
            try:
                providers = [p["id"] for p in GATE.models_used()]
            except Exception:  # noqa: BLE001
                providers = []
        return {"profiles": profiles, "providers": providers,
                "internet": bool(req.get("internet")),
                "llm": bool(req.get("llm")),
                "offline_capable": bool(req.get("offline_capable", True)),
                "sources": [str(s.get("id") or "") for s in (req.get("sources") or [])
                            if isinstance(s, dict) and s.get("id")]}
    except Exception:  # noqa: BLE001 — подсказка не имеет права ломать снимок
        return {"profiles": [], "providers": [], "internet": False, "llm": False,
                "offline_capable": True, "sources": []}


def parse_scutil(text: str) -> list:
    """Активные VPN-сервисы macOS из `scutil --nc list` → [{name, kind}].

    Храним имя сервиса: по нему видно, какой именно провайдер остался без связи.
    UUID и адреса не нужны и не сохраняются.
    """
    out = []
    for line in str(text or "").splitlines():
        if "(Connected)" not in line:
            continue
        name = re.search(r'"([^"]+)"', line)
        kind = re.search(r"\[([^\]]+)\]\s*$", line)
        if name:
            out.append({"name": _clean_name(name.group(1)),
                        "kind": _clean_name(kind.group(1), 40) if kind else ""})
    return out


def parse_windows_vpn(text: str) -> list:
    """Активные VPN Windows из JSON `Get-VpnConnection` (или пусто)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("ConnectionStatus") or "").lower() != "connected":
            continue
        name = _clean_name(row.get("Name"))
        if name:
            out.append({"name": name, "kind": "vpn"})
    return out


def parse_nmcli(text: str) -> list:
    """Активные VPN-подключения Linux из `nmcli -t`."""
    out = []
    for line in str(text or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] in ("vpn", "wireguard"):
            out.append({"name": _clean_name(parts[0]), "kind": parts[1]})
    return out


def system_vpn_state() -> dict:
    """Подключённые системные VPN. Ошибка команды — `unknown`, а не исключение."""
    row = {"platform": sys.platform, "source": "", "connected": None, "services": []}
    if (os.environ.get("AURORA_NET_SYSTEM_VPN", "1") == "0"
            or os.environ.get("AURORA_TESTS_ISOLATED")):
        row["source"] = "off"
        return row
    try:
        if sys.platform == "darwin":
            cp = subprocess.run(["scutil", "--nc", "list"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=SYSTEM_TIMEOUT)
            row["source"] = "scutil"
            row["services"] = parse_scutil(cp.stdout or "") if cp.returncode == 0 else []
        elif sys.platform == "win32":
            script = ("Get-VpnConnection | Where-Object {$_.ConnectionStatus -eq 'Connected'} | "
                      "Select-Object Name,ServerAddress,ConnectionStatus | ConvertTo-Json -Compress")
            cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=SYSTEM_TIMEOUT)
            row["source"] = "powershell"
            row["services"] = parse_windows_vpn(cp.stdout or "") if cp.returncode == 0 else []
        elif sys.platform.startswith("linux"):
            cp = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=SYSTEM_TIMEOUT)
            row["source"] = "nmcli"
            row["services"] = parse_nmcli(cp.stdout or "") if cp.returncode == 0 else []
        else:
            row["source"] = "unknown"
    except (OSError, subprocess.SubprocessError, ValueError):
        row["source"] = row["source"] or "unknown"
        row["services"] = []
    row["connected"] = len(row["services"]) if row["source"] not in ("", "unknown") else None
    return row


def _compact(snap: dict, req: dict) -> dict:
    """Только то, что нужно для разбора: без probe-адресов, ключей и путей."""
    profiles = []
    for row in snap.get("profiles") or []:
        profiles.append({"name": _clean_name(row.get("name")),
                         "state": row.get("state", "unknown"),
                         "since": row.get("since", ""),
                         "checked": row.get("checked", ""),
                         "age_s": row.get("age_s"),
                         "healthy": row.get("healthy"),
                         "total": row.get("total"),
                         "required": bool(row.get("required"))})
    providers = []
    for row in snap.get("providers") or []:
        providers.append({"id": _clean_name(row.get("id"), 80),
                          "name": _clean_name(row.get("name")),
                          "state": row.get("state", "unknown"),
                          "why": _clean_name(row.get("why"), 80),
                          "profile": _clean_name(row.get("profile")),
                          "required": bool(row.get("required"))})
    net = snap.get("internet") or {}
    blocked = []
    try:
        import conn_gate as GATE
        verdict = GATE.evaluate(req, snap) if req else {"blocked": []}
        blocked = [{"code": _clean_name(x.get("code"), 40),
                    "name": _clean_name(x.get("name")),
                    "providers": [str(p) for p in (x.get("providers") or [])],
                    "why": [str(w) for w in (x.get("why") or [])]} for x in verdict.get("blocked") or []]
    except Exception:  # noqa: BLE001
        blocked = []
    return {"state": snap.get("state", "unknown"),
            "enabled": bool(snap.get("enabled")),
            "profiles": profiles,
            "providers": providers,
            "internet": {"state": net.get("state", "unknown"),
                         "monitored": bool(net.get("monitored"))},
            "requirements": req,
            "blocked": blocked}


def snapshot(project: str, cmd: str = "", parent: str = "",
             steps: list | None = None, scope: str = "start", elapsed_s: int = 0) -> dict:
    """Компактный снимок текущего состояния. Сеть не опрашивает: берёт уже посчитанное."""
    req = _requirements(project, cmd, steps)
    try:
        import connectivity as CN
        svc = CN.service()
        snap = svc.snapshot({"profiles": req["profiles"], "providers": req["providers"],
                             "internet": req["internet"]})
    except Exception as exc:  # noqa: BLE001 — снимок не имеет права ломать прогон
        snap = {"state": "unknown", "enabled": False, "profiles": [], "providers": [],
                "internet": {"state": "unknown", "monitored": False}, "error": type(exc).__name__}
    out = {"at": _now_stamp(), "scope": scope, "elapsed_s": int(max(0, elapsed_s)),
           "project": _project_name(project), "cmd": _clean_name(cmd, 160),
           "parent": _clean_name(parent, 120)}
    out.update(_compact(snap, req))
    out["system_vpn"] = system_vpn_state()
    return out


def _base(kind: str, run_id: str, project: str, cmd: str = "", parent: str = "",
          started: str = "") -> dict:
    return {"version": 1, "kind": kind, "id": run_id, "project": _project_name(project),
            "cmd": _clean_name(cmd, 160), "parent": _clean_name(parent, 120),
            "started": started or _now_stamp(), "finished": "", "snapshots": []}


def _append_snapshot(path: Path, snap: dict) -> dict:
    data = _read(path)
    if not data:
        return {}
    data.setdefault("snapshots", []).append(snap)
    data["snapshots"] = data["snapshots"][-MAX_SNAPSHOTS:]
    data["summary"] = summarize(data)
    _write(path, data)
    return data


def prune_shared(folder: str | os.PathLike, keep: int = KEEP_RECORDS,
                 days: int = KEEP_DAYS, now: float | None = None,
                 active: str | os.PathLike | None = None) -> list[str]:
    """Убрать старые снимки своего шарда: не больше `keep` записей и не старше `days`.

    Чужой шард не трогаем: вызывающий передаёт каталог `<actor>-<machine>`. Файл,
    который только что записан (`active`), не удаляется. Ошибки чтения и удаления
    не поднимаются: уборка не имеет права ломать прогон.
    """
    folder = Path(folder)
    keep = max(0, int(keep))
    cutoff = (time.time() if now is None else float(now)) - max(0, int(days)) * 86400
    rows: list[tuple[float, str, Path]] = []
    try:
        for path in folder.glob("*.json"):
            try:
                rows.append((path.stat().st_mtime, path.name, path))
            except OSError:
                continue
    except OSError:
        return []
    rows.sort()
    doomed = {path for _mtime, _name, path in (rows if not keep else rows[:-keep])}
    doomed |= {path for mtime, _name, path in rows if mtime < cutoff}
    if active is not None:
        active_key = os.path.normcase(os.path.abspath(str(active)))
        doomed = {path for path in doomed
                  if os.path.normcase(os.path.abspath(str(path))) != active_key}
    gone: list[str] = []
    for path in sorted(doomed):
        try:
            path.unlink(missing_ok=True)
            gone.append(path.name)
        except OSError:
            continue
    return gone


def start(kind: str, project: str, run_id: str, run_dir: str, cmd: str = "",
          parent: str = "", steps: list | None = None) -> dict:
    """Снимок старта; локальный файл создаётся сразу."""
    if not enabled():
        return {}
    path = local_path(run_dir)
    data = _base(kind, run_id, project, cmd, parent)
    data["snapshots"].append(snapshot(project, cmd=cmd, parent=parent, steps=steps,
                                      scope="start", elapsed_s=0))
    data["summary"] = summarize(data)
    try:
        _write(path, data)
    except OSError:
        pass
    return data


def long_snapshot(kind: str, project: str, run_id: str, run_dir: str, cmd: str = "",
                  parent: str = "", elapsed_s: int = 0, steps: list | None = None) -> dict:
    """Одна отметка долгой операции; без файла старта создаёт его."""
    if not enabled() or not run_dir:
        return {}
    path = local_path(run_dir)
    with _LOCK:
        data = _read(path)
        if not data:
            data = _base(kind, run_id, project, cmd, parent)
        data["snapshots"].append(snapshot(project, cmd=cmd, parent=parent, steps=steps,
                                          scope="long", elapsed_s=elapsed_s))
        data["snapshots"] = data["snapshots"][-MAX_SNAPSHOTS:]
        data["summary"] = summarize(data)
        try:
            _write(path, data)
        except OSError:
            pass
        return data


def finish(kind: str, project: str, run_id: str, run_dir: str, cmd: str = "",
           parent: str = "", started: float | None = None, steps: list | None = None,
           share: bool | None = None) -> dict:
    """Снимок конца, итог и (для верхнего уровня или долгой операции) общий файл."""
    if not enabled():
        return {}
    path = local_path(run_dir)
    with _LOCK:
        data = _read(path)
        if not data:
            data = _base(kind, run_id, project, cmd, parent,
                         _iso(started) if started else "")
        elapsed = int(max(0, time.time() - started)) if started else 0
        data["snapshots"].append(snapshot(project, cmd=cmd, parent=parent, steps=steps,
                                          scope="end", elapsed_s=elapsed))
        data["snapshots"] = data["snapshots"][-MAX_SNAPSHOTS:]
        data["finished"] = _now_stamp()
        data["seconds"] = elapsed
        data["summary"] = summarize(data)
        try:
            _write(path, data)
        except OSError:
            pass
        auto_share = not parent or bool([x for x in data["snapshots"] if x.get("scope") == "long"])
        if (share is True) or (share is None and auto_share):
            sp = shared_path(project, run_id)
            if sp is not None:
                try:
                    _write(sp, data)
                except OSError:
                    pass
                else:
                    prune_shared(sp.parent, active=sp)
        return data


def peek(kind: str, project: str, run_id: str, run_dir: str) -> dict:
    """Текущая сводка без снимка конца: нужна до печати итога, а `finish` уже закрыл бы окно."""
    if not enabled():
        return {}
    data = _read(local_path(run_dir))
    if not data:
        return {}
    summary = summarize(data)
    return {"summary": summary, "timing": summary.get("timing") or {}}


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def summarize(data: dict) -> dict:
    """Простые секунды: в каком состоянии шаг провёл время и что его блокировало.

    Это приближение по снимкам: между двумя снимками состояние считается неизменным.
    Точные переходы профиля лежат в `connectivity-events.jsonl` на машине.
    """
    snaps = [s for s in (data.get("snapshots") or []) if isinstance(s, dict)]
    if not snaps:
        return {"seconds": int(data.get("seconds") or 0), "snapshots": 0,
                "profiles": {}, "providers": {}, "blocked_s": 0,
                "network_required": False, "reason": ""}
    total = int(data.get("seconds") or 0)
    if not total:
        first, last = _parse(snaps[0].get("at", "")), _parse(snaps[-1].get("at", ""))
        if first and last:
            total = max(0, int((last - first).total_seconds()))
    profiles: dict[str, int] = {}
    providers: dict[str, int] = {}
    blocked_s = 0
    states = set()
    network_required = False
    for i, snap in enumerate(snaps):
        start = _parse(snap.get("at", ""))
        end = _parse(snaps[i + 1].get("at", "")) if i + 1 < len(snaps) else None
        if end is None and total:
            end = start + timedelta(seconds=max(0, total - int(snap.get("elapsed_s") or 0))) if start else None
        sec = max(0, int((end - start).total_seconds())) if start and end else int(snap.get("elapsed_s") or 0)
        req = snap.get("requirements") or {}
        network_required = network_required or bool(req.get("llm") or req.get("profiles") or req.get("internet"))
        states.add(str(snap.get("state") or "unknown"))
        if snap.get("blocked"):
            blocked_s += sec
        for row in snap.get("profiles") or []:
            if row.get("required") and row.get("state") == "offline":
                name = str(row.get("name") or "")
                profiles[name] = profiles.get(name, 0) + sec
        for row in snap.get("providers") or []:
            if row.get("required") and row.get("state") == "offline":
                name = str(row.get("id") or "")
                providers[name] = providers.get(name, 0) + sec
    last = snaps[-1]
    if not network_required:
        reason = "local"
    elif last.get("blocked"):
        reason = "network"
    elif last.get("state") == "unknown":
        reason = "unknown"
    else:
        reason = "ok"
    out = {"seconds": total, "snapshots": len(snaps),
           "profiles": dict(sorted(profiles.items())),
           "providers": dict(sorted(providers.items())),
           "blocked_s": blocked_s, "network_required": network_required,
           "states": sorted(states), "reason": reason}
    try:
        import timing as T
        # Снимки дают оценку, а не точные интервалы: `~` и `approx` обязательны.
        out["timing"] = T.legacy_timing(total, out)
    except Exception:  # noqa: BLE001 — старая копия кита без timing не ломает отчёт
        pass
    return out


def load_run(project: str, run_id: str) -> dict:
    """Локальный файл прогона, иначе общий файл любого участника."""
    local = local_path(Path(project) / ".aurora" / "runs" / run_id)
    data = _read(local)
    if data:
        return data
    root = Path(project).expanduser().resolve().joinpath(*SHARED_DIR)
    if not root.is_dir():
        return {}
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        data = _read(folder / f"{run_id}.json")
        if data:
            return data
    return {}


def line(snap: dict) -> str:
    """Одна строка в консоль для долгой операции: что с сетью, VPN и LLM."""
    mins = int(snap.get("elapsed_s") or 0) // 60
    parts = [f"⏱ {mins} мин · сеть: {snap.get('state', 'unknown')}"]
    profiles = [p for p in snap.get("profiles") or [] if p.get("required")]
    if profiles:
        parts.append("профили: " + ", ".join(
            f"{p.get('name')}={p.get('state')}" for p in profiles))
    services = (snap.get("system_vpn") or {}).get("services") or []
    if services:
        parts.append("системный VPN: " + ", ".join(
            f"{s.get('name')} ({s.get('kind') or 'vpn'})" for s in services))
    down = [p for p in snap.get("providers") or []
            if p.get("required") and p.get("state") == "offline"]
    if down:
        parts.append("LLM: " + ", ".join(
            f"{p.get('name') or p.get('id')}={p.get('why') or 'offline'}" for p in down))
    if snap.get("blocked"):
        parts.append("блокирует: " + ", ".join(
            str(x.get("code") or x.get("name") or "") for x in snap["blocked"]))
    return " · ".join(parts)[:700]


class Observer:
    """Один фоновый обход активных заданий и маршрутов, а не поток на каждую операцию."""

    def __init__(self, jobs_getter, routes_getter=None, on_long=None,
                 clock=time.time, interval: float = 30.0):
        self.jobs_getter = jobs_getter
        self.routes_getter = routes_getter
        self.on_long = on_long
        self.clock = clock
        self.interval = interval
        self.last: dict[str, float] = {}
        self.count: dict[str, int] = {}
        self._stop = threading.Event()
        self._thread = None

    def tick(self, now: float | None = None) -> int:
        if not enabled():
            return 0
        now = self.clock() if now is None else now
        written = 0
        jobs = list(self.jobs_getter() or [])
        live = set()
        for job in jobs:
            if not isinstance(job, dict) or job.get("done"):
                continue
            run_id = str(job.get("run_id") or "")
            if not run_id:
                continue
            started = job.get("started") or 0
            elapsed = now - float(started or 0)
            if elapsed < long_after():
                continue
            key = "job:" + run_id
            live.add(key)
            previous = self.last.get(key)
            if previous is not None and elapsed - previous < long_every():
                continue
            if self.count.get(key, 0) >= MAX_LONG:
                continue
            if not job.get("run_dir"):
                continue
            data = long_snapshot("job", str(job.get("project") or ""), run_id,
                                 str(job.get("run_dir") or ""), str(job.get("cmd") or ""),
                                 str(job.get("parent") or ""), int(elapsed))
            job["net_long"] = int(job.get("net_long") or 0) + 1
            self.last[key] = elapsed
            self.count[key] = self.count.get(key, 0) + 1
            written += 1
            if self.on_long and data.get("snapshots"):
                try:
                    self.on_long(job, data["snapshots"][-1])
                except Exception:  # noqa: BLE001
                    pass
        if self.routes_getter:
            for route in list(self.routes_getter() or []):
                if not isinstance(route, dict):
                    continue
                run = route.get("run")
                run_id = str(route.get("id") or getattr(run, "run_id", "") or "")
                if not run or not run_id:
                    continue
                if getattr(run, "done_flag", False):
                    continue
                started = float(getattr(run, "started", 0) or 0)
                elapsed = now - started
                if elapsed < long_after():
                    continue
                key = "route:" + run_id
                live.add(key)
                previous = self.last.get(key)
                if previous is not None and elapsed - previous < long_every():
                    continue
                if not getattr(run, "base", ""):
                    continue
                if self.count.get("route:" + run_id, 0) >= MAX_LONG:
                    continue
                sc = getattr(run, "sc", {}) or {}
                data = long_snapshot("route", str(getattr(run, "project", "") or ""), run_id,
                                     str(getattr(run, "base", "") or ""),
                                     str(route.get("id") or ""), "",
                                     int(elapsed), steps=[s.get("cmd") for s in sc.get("steps", [])])
                self.last[key] = elapsed
                self.count[key] = self.count.get(key, 0) + 1
                written += 1
                if self.on_long and data.get("snapshots"):
                    try:
                        self.on_long({"route": route}, data["snapshots"][-1])
                    except Exception:  # noqa: BLE001
                        pass
        for key in list(self.last):
            if key not in live:
                self.last.pop(key, None)
                self.count.pop(key, None)
        return written

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    pass

        self._thread = threading.Thread(target=loop, daemon=True, name="aurora-net-observer")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def ensure_started(jobs_getter, routes_getter=None, on_long=None) -> Observer:
    global _OBSERVER
    with _LOCK:
        if _OBSERVER is None:
            _OBSERVER = Observer(jobs_getter, routes_getter, on_long)
            if enabled():
                _OBSERVER.start()
        return _OBSERVER
