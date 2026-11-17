#!/usr/bin/env python3
"""usage_log.py — расход токенов по проектам, ролям агента и моделям.

Пишется после прогона агента в часть участника и машины:
`AuroraKnowledgeDB/meta/usage.d/<actor>-<machine>/<run_id>.json`. Два участника никогда
не пишут в один файл, поэтому merge не конфликтует. Записи старше 90 дней удаляются при
следующей записи — история нужна для «Замеров», а не как вечный архив.

Наружу уходят только числа: проект — имя папки, роль, модель, вызовы, токены и время.
Ни промптов, ни ключей, ни адресов здесь нет.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from aurora_actor import actor
from aurora_common import utc_stamp
from aurora_fs import write_json

SHARED_DIR = ("AuroraKnowledgeDB", "meta", "usage.d")
KEEP_DAYS = 90
RUN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_COUNTERS = ("calls", "tokens_in", "tokens_out", "seconds", "errors")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _machine_id() -> str:
    """Короткий стабильный идентификатор машины, как у снимков сети."""
    import hashlib
    import platform
    raw = os.environ.get("AURORA_MACHINE") or platform.node() or "machine"
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")[:48] or "machine"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _project_name(project: str) -> str:
    return os.path.basename(os.path.normpath(str(project or ""))) or ""


def _folder(project: str) -> Path | None:
    root = Path(project).expanduser().resolve()
    if not (root / "AuroraKnowledgeDB").is_dir():
        return None
    actor_id, _display = actor(str(root))
    return root.joinpath(*SHARED_DIR) / f"{actor_id}-{_machine_id()}"


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()[:120]


def _counter(row: dict) -> dict:
    out = {k: 0 for k in _COUNTERS}
    for k in _COUNTERS:
        try:
            out[k] = int(row.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    out["seconds"] = round(float(row.get("seconds") or 0), 2)
    return out


def _add(dst: dict, row: dict) -> None:
    for k in _COUNTERS:
        if k == "seconds":
            dst["seconds"] = round(dst.get("seconds", 0) + float(row.get(k) or 0), 2)
        else:
            dst[k] = dst.get(k, 0) + int(row.get(k) or 0)


def _prune(folder: Path, days: int = KEEP_DAYS) -> int:
    cutoff = time.time() - days * 86400
    gone = 0
    try:
        for path in folder.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    gone += 1
            except OSError:
                continue
    except OSError:
        pass
    return gone


def record(project: str, run_id: str, data: dict, who: str = "") -> dict:
    """Записать итог одного прогона. Ошибка записи не роняет прогон: замер — сведение."""
    folder = _folder(project)
    if folder is None or not RUN_RE.fullmatch(str(run_id or "")):
        return {}
    payload = {
        "schema": 1,
        "at": _now_stamp(),
        "project": _project_name(project),
        "run": str(run_id),
        "kind": _clean((data or {}).get("kind") or "agent"),
        "task": _clean((data or {}).get("task") or ""),
        "started": str((data or {}).get("started") or ""),
        "seconds": round(float((data or {}).get("seconds") or 0), 2),
        "calls": int((data or {}).get("calls") or 0),
        "tokens_in": int((data or {}).get("tokens_in") or 0),
        "tokens_out": int((data or {}).get("tokens_out") or 0),
        "errors": int((data or {}).get("errors") or 0),
        "estimated": bool((data or {}).get("estimated")),
        "by_role": {},
        "by_model": {},
        "by_pair": {},
    }
    for field in ("by_role", "by_model", "by_pair"):
        raw = (data or {}).get(field) or {}
        if not isinstance(raw, dict):
            continue
        for name, row in raw.items():
            key = _clean(name)
            if key and isinstance(row, dict):
                payload[field][key] = _counter(row)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        write_json(str(folder / f"{run_id}.json"), payload, indent=1)
        _prune(folder)
    except OSError:
        return {}
    return payload


def iter_records(project: str, since: float = 0.0) -> list:
    """Все записи проекта с общих частей участников, свежие первыми. → [payload]."""
    folder = Path(project).expanduser().resolve().joinpath(*SHARED_DIR)
    if not folder.is_dir():
        return []
    out = []
    try:
        for part in folder.iterdir():
            if not part.is_dir():
                continue
            for path in part.glob("*.json"):
                try:
                    if path.stat().st_mtime < since:
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict) and data.get("run"):
                    out.append(data)
    except OSError:
        return []
    out.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return out


def _parse_at(value: str) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def summary(projects, days: int = 7, now: float | None = None) -> dict:
    """Сводка по проектам, ролям и моделям. `days=0` — всё, что лежит (не старше 90 дней)."""
    now = time.time() if now is None else float(now)
    days = max(0, int(days or 0))
    since = now - days * 86400 if days else 0.0
    totals = {k: 0 for k in _COUNTERS}
    by_project: dict = {}
    by_role: dict = {}
    by_model: dict = {}
    by_task: dict = {}
    rows: dict = {}
    old = 0
    records = 0
    for project in projects or []:
        for rec in iter_records(project, since=since):
            if since and _parse_at(rec.get("at")) and _parse_at(rec.get("at")) < since:
                continue
            records += 1
            pname = _clean(rec.get("project") or _project_name(project))
            base = _counter(rec)
            _add(totals, base)
            _add(by_project.setdefault(pname, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                               "seconds": 0.0, "errors": 0}), base)
            task = _clean(rec.get("task") or "—")
            _add(by_task.setdefault(task, {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                           "seconds": 0.0, "errors": 0}), base)
            roles = rec.get("by_role") or {}
            models = rec.get("by_model") or {}
            pairs = rec.get("by_pair") or {}
            if not roles and not models and not pairs:
                old += 1
            if not roles:
                roles = {"—": base}
            if not models:
                models = {"—": base}
            for role, row in roles.items():
                _add(by_role.setdefault(_clean(role) or "—", {
                    "calls": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0,
                    "errors": 0}), _counter(row))
            for model, row in models.items():
                _add(by_model.setdefault(_clean(model) or "—", {
                    "calls": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0.0,
                    "errors": 0}), _counter(row))
            if pairs:
                for pair, row in pairs.items():
                    role, _sep, model = str(pair).partition("|")
                    key = (pname, _clean(role) or "—", _clean(model) or "—")
                    _add(rows.setdefault(key, {"project": key[0], "role": key[1],
                                               "model": key[2], "calls": 0,
                                               "tokens_in": 0, "tokens_out": 0,
                                               "seconds": 0.0, "errors": 0}), _counter(row))
            else:
                # Старая запись без пар: показываем по ролям, модель — «—». Полный итог
                # не размножаем на каждую роль и модель: сумма разошлась бы с totals.
                for role in roles:
                    key = (pname, _clean(role) or "—", "—")
                    _add(rows.setdefault(key, {"project": key[0], "role": key[1],
                                               "model": key[2], "calls": 0,
                                               "tokens_in": 0, "tokens_out": 0,
                                               "seconds": 0.0, "errors": 0}), base)
    def table(src):
        return [dict(name=k, **v) for k, v in sorted(
            src.items(), key=lambda kv: (-(kv[1].get("tokens_in", 0) + kv[1].get("tokens_out", 0)),
                                         kv[0]))]
    return {
        "records": records,
        "old": old,
        "since": (utc_stamp(since) if since else ""),
        "totals": {**totals, "estimated": bool(old and old == records)},
        "projects": table(by_project),
        "roles": table(by_role),
        "models": table(by_model),
        "tasks": table(by_task),
        "rows": [dict(v) for _k, v in sorted(
            rows.items(), key=lambda kv: (-(kv[1].get("tokens_in", 0) + kv[1].get("tokens_out", 0)),
                                          kv[0]))],
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="usage_log.py", description="Токены по проектам и моделям.")
    ap.add_argument("--project", action="append", default=[], help="корень проекта (можно несколько)")
    ap.add_argument("--days", type=int, default=7, help="0 — всё, что лежит (до 90 дней)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    got = summary(a.project, a.days)
    if a.json:
        print(json.dumps(got, ensure_ascii=False, indent=1))
    else:
        t = got["totals"]
        print(f"Записей: {got['records']} · вызовов: {t['calls']} · "
              f"токены: вход {t['tokens_in']}, выход {t['tokens_out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
