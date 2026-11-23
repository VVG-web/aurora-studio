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
from aurora_common import utc_stamp, yaml_scalar
from aurora_fs import write_json

SHARED_DIR = ("AuroraKnowledgeDB", "meta", "usage.d")
KEEP_DAYS = 90
RUN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")

_INT_FIELDS = ("calls", "calls_failed", "requests", "tokens_in", "tokens_out",
               "tokens_failed_in", "tokens_failed_out", "cache_hits",
               "usage_missing_calls", "errors")
_FLOAT_FIELDS = ("seconds", "gen_seconds")
_NULLABLE_FIELDS = ("cached_in", "reasoning_out")
_COUNTERS = _INT_FIELDS + _FLOAT_FIELDS


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


def _clean(name: str, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()[:limit]


def _project_id(project: str) -> str:
    """Slug из `project:` в aurora.config.yaml; иначе имя папки. Без пути наружу."""
    root = Path(project).expanduser().resolve()
    name = _project_name(project)
    slug = ""
    try:
        text = (root / "aurora.config.yaml").read_text(encoding="utf-8", errors="ignore")
        block = text
        found = re.search(r"(?ms)^project:\s*$(.*?)(?=^\S|\Z)", text)
        if found:
            block = found.group(1)
        slug = yaml_scalar(block, "slug", "")
    except OSError:
        pass
    value = _clean(slug or name)
    value = value.replace("/", "-").replace("\\", "-").strip()
    return value or name


def _int(value, default: int = 0) -> int:
    try:
        return max(0, int(value if value is not None else default))
    except (TypeError, ValueError):
        return default


def _float(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _nullable(value):
    if value in (None, ""):
        return None
    return _int(value)


def _counter(row: dict) -> dict:
    row = row if isinstance(row, dict) else {}
    out = {k: 0 for k in _INT_FIELDS}
    for k in _INT_FIELDS:
        out[k] = _int(row.get(k))
    for k in _FLOAT_FIELDS:
        out[k] = _float(row.get(k))
    for k in _NULLABLE_FIELDS:
        out[k] = _nullable(row.get(k))
    return out


def _add(dst: dict, row: dict) -> None:
    for k in _INT_FIELDS:
        dst[k] = _int(dst.get(k)) + _int((row or {}).get(k))
    for k in _FLOAT_FIELDS:
        dst[k] = round(float(dst.get(k) or 0) + _float((row or {}).get(k)), 2)
    for k in _NULLABLE_FIELDS:
        value = _nullable((row or {}).get(k))
        if value is not None:
            dst[k] = _int(dst.get(k)) + value
        else:
            dst.setdefault(k, None)


def _totals_view(row: dict) -> dict:
    out = dict(row)
    out["tokens_total"] = _int(out.get("tokens_in")) + _int(out.get("tokens_out"))
    out["tokens_failed"] = (_int(out.get("tokens_failed_in"))
                            + _int(out.get("tokens_failed_out")))
    out["tokens_all"] = out["tokens_total"] + out["tokens_failed"]
    return out


def _cell(row: dict, key: tuple | None = None) -> dict | None:
    row = row if isinstance(row, dict) else {}
    provider = row.get("provider")
    role = row.get("role")
    model = row.get("model")
    if key and len(key) == 3:
        provider = key[0] if provider is None else provider
        role = key[1] if role is None else role
        model = key[2] if model is None else model
    data = {
        "provider": _clean(provider, 80) or None,
        "provider_name": _clean(row.get("provider_name"), 120) or None,
        "role": _clean(role, 80) or None,
        "model": _clean(model, 120) or None,
        "calls": _int(row.get("calls")),
        "calls_failed": _int(row.get("calls_failed")),
        "requests": _int(row.get("requests")),
        "tokens_in": _int(row.get("tokens_in")),
        "tokens_out": _int(row.get("tokens_out")),
        "cached_in": _nullable(row.get("cached_in")),
        "reasoning_out": _nullable(row.get("reasoning_out")),
        "gen_seconds": _float(row.get("gen_seconds")),
    }
    if not any(data[k] for k in ("calls", "calls_failed", "requests", "tokens_in",
                                 "tokens_out", "gen_seconds")) \
            and data["cached_in"] is None and data["reasoning_out"] is None:
        return None
    return data


def _cells(data: dict) -> list:
    raw = (data or {}).get("by_cell")
    out: list = []
    if isinstance(raw, dict):
        for key, row in raw.items():
            cell = _cell(row if isinstance(row, dict) else {},
                         key if isinstance(key, tuple) else None)
            if cell:
                out.append(cell)
    elif isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict):
                cell = _cell(row)
                if cell:
                    out.append(cell)
    out.sort(key=lambda c: (-(c["tokens_in"] + c["tokens_out"]),
                            c["provider"] or "", c["role"] or "", c["model"] or ""))
    return out[:256]


def _legacy(data: dict, field: str) -> dict:
    raw = (data or {}).get(field) or {}
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for name, row in raw.items():
        key = _clean(name)
        if key and isinstance(row, dict):
            out[key] = _counter(row)
    return out


def _prune(folder: Path, days: int = KEEP_DAYS) -> int:
    """Удалить записи по `at`. Для битого/старого файла без `at` — запасной mtime."""
    cutoff = time.time() - days * 86400
    gone = 0
    try:
        for path in folder.glob("*.json"):
            try:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    stamp = _parse_at(data.get("at")) if isinstance(data, dict) else 0.0
                except (OSError, ValueError):
                    stamp = 0.0
                if stamp <= 0:
                    stamp = path.stat().st_mtime
                if stamp < cutoff:
                    path.unlink()
                    gone += 1
            except OSError:
                continue
    except OSError:
        pass
    return gone


def record(project: str, run_id: str, data: dict, who: str = "") -> dict:
    """Записать итог одного прогона в schema 2. Ошибка записи не роняет прогон."""
    folder = _folder(project)
    if folder is None or not RUN_RE.fullmatch(str(run_id or "")):
        return {}
    data = data if isinstance(data, dict) else {}
    started = data.get("started")
    started = utc_stamp(started) if started not in (None, "") else ""
    calls = data.get("calls")
    if calls is None and data.get("model_calls") is not None:
        calls = max(0, _int(data.get("model_calls")) - _int(data.get("calls_failed")))
    errors = data.get("errors")
    if isinstance(errors, dict):
        errors = sum(_int(v) for v in errors.values())
    payload = {
        "schema": 2,
        "at": _now_stamp(),
        "started": started,
        "seconds": _float(data.get("seconds")),
        "project": _clean(_project_name(project)),
        "project_id": _project_id(project),
        "run": str(run_id),
        "kind": _clean(data.get("kind") or "agent", 40),
        "task": _clean(data.get("task") or ""),
        "calls": _int(calls),
        "calls_failed": _int(data.get("calls_failed", data.get("failed", 0))),
        "requests": _int(data.get("requests")),
        "tokens_in": _int(data.get("tokens_in")),
        "tokens_out": _int(data.get("tokens_out")),
        "tokens_failed_in": _int(data.get("tokens_failed_in")),
        "tokens_failed_out": _int(data.get("tokens_failed_out")),
        "cached_in": _nullable(data.get("cached_in")),
        "reasoning_out": _nullable(data.get("reasoning_out")),
        "cache_hits": _int(data.get("cache_hits", data.get("cached", 0))),
        "usage_missing_calls": _int(data.get("usage_missing_calls")),
        "gen_seconds": _float(data.get("gen_seconds")),
        "estimated": (bool(data.get("estimated"))
                      or bool(_int(data.get("usage_missing_calls")))),
        "errors": _int(errors),
        "by_cell": _cells(data),
        "by_role": _legacy(data, "by_role"),
        "by_model": _legacy(data, "by_model"),
        "by_pair": _legacy(data, "by_pair"),
    }
    try:
        folder.mkdir(parents=True, exist_ok=True)
        write_json(str(folder / f"{run_id}.json"), payload, indent=1)
        _prune(folder)
    except OSError:
        return {}
    return payload


def iter_records(project: str, since: float = 0.0) -> list:
    """Все записи проекта с общих частей участников, свежие первыми. → [payload].

    Читает schema 1 и schema 2. Schema 1 не переписывается: недостающие поля добирает
    `summary`. Битый JSON пропускается. Наружу не отдаётся путь файла.
    """
    root = Path(project).expanduser().resolve()
    folder = root.joinpath(*SHARED_DIR)
    if not folder.is_dir():
        return []
    pname = _project_name(project)
    out = []
    try:
        for part in folder.iterdir():
            if not part.is_dir():
                continue
            for path in part.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict) or not data.get("run"):
                    continue
                try:
                    schema = int(data.get("schema") or 1)
                except (TypeError, ValueError):
                    schema = 1
                rec = dict(data)
                rec["schema"] = schema if schema in (1, 2) else 1
                if not rec.get("project"):
                    rec["project"] = pname
                if not rec.get("project_id"):
                    rec["project_id"] = _clean(rec.get("project") or pname)
                stamp = _parse_at(rec.get("at"))
                if since and (stamp or path.stat().st_mtime) < since:
                    continue
                out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: (_parse_at(r.get("at")), str(r.get("at") or "")), reverse=True)
    return out


def _parse_at(value: str) -> float:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _blank() -> dict:
    return _counter({})


def summary(projects, days: int = 7, now: float | None = None) -> dict:
    """Сводка по проектам, ролям и моделям. `days=0` — окно хранения 90 дней."""
    now = time.time() if now is None else float(now)
    days = max(0, int(days or 0))
    window = days or KEEP_DAYS
    cutoff = now - window * 86400
    since = cutoff if days else 0.0

    # Сначала дедупликация (project_id, run), затем окно: повтор старой копии не должен
    # скрыть свежую запись того же прогона.
    chosen: dict = {}
    for project in projects or []:
        pname = _project_name(project)
        for rec in iter_records(project, since=0.0):
            run = str(rec.get("run") or "")
            if not run:
                continue
            pid = _clean(rec.get("project_id") or rec.get("project") or pname)
            at = _parse_at(rec.get("at"))
            key = (pid, run)
            if key not in chosen or at > chosen[key][0]:
                chosen[key] = (at, rec, pname)

    totals = _blank()
    by_project: dict = {}
    by_provider: dict = {}
    by_role: dict = {}
    by_model: dict = {}
    by_task: dict = {}
    rows: dict = {}
    old = 0
    old_schema = 0
    estimated = 0
    no_calls = 0
    records = 0
    for at, rec, pname in chosen.values():
        if at and at < cutoff:
            continue
        records += 1
        if rec.get("schema") == 1:
            old_schema += 1
        if rec.get("schema") == 1 or rec.get("estimated"):
            estimated += 1
        base = _counter(rec)
        if not (base["calls"] or base["calls_failed"] or base["requests"]):
            no_calls += 1
        _add(totals, base)
        pname = _clean(rec.get("project") or pname)
        pid = _clean(rec.get("project_id") or pname)
        project_row = by_project.setdefault(pname, {**_blank(), "project_id": pid})
        _add(project_row, base)
        task = _clean(rec.get("task") or "—")
        _add(by_task.setdefault(task, _blank()), base)

        cells = rec.get("by_cell")
        cells = cells if isinstance(cells, list) else []
        pairs = rec.get("by_pair") if isinstance(rec.get("by_pair"), dict) else {}
        roles = rec.get("by_role") if isinstance(rec.get("by_role"), dict) else {}
        models = rec.get("by_model") if isinstance(rec.get("by_model"), dict) else {}
        used_breakdown = bool(cells or pairs or roles or models)
        if not used_breakdown:
            old += 1
        if cells:
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                row = _counter(cell)
                role = _clean(cell.get("role") or "—")
                model = _clean(cell.get("model") or "—")
                provider = _clean(cell.get("provider")) or ""
                provider_name = _clean(cell.get("provider_name"))
                _add(by_role.setdefault(role, _blank()), row)
                _add(by_model.setdefault(model, _blank()), row)
                if provider:
                    prow = by_provider.setdefault(provider, {**_blank(),
                                                              "provider_name": provider_name})
                    _add(prow, row)
                key = (pname, provider or None, role, model)
                _add(rows.setdefault(key, {**_blank(), "project": pname,
                                           "provider": provider or None,
                                           "provider_name": provider_name or None,
                                           "role": role, "model": model}), row)
        elif pairs:
            for pair, value in pairs.items():
                role, _sep, model = str(pair).partition("|")
                role, model = _clean(role or "—"), _clean(model or "—")
                row = _counter(value)
                _add(by_role.setdefault(role, _blank()), row)
                _add(by_model.setdefault(model, _blank()), row)
                key = (pname, None, role, model)
                _add(rows.setdefault(key, {**_blank(), "project": pname,
                                           "provider": None, "provider_name": None,
                                           "role": role, "model": model}), row)
        elif roles:
            for role, value in roles.items():
                role = _clean(role or "—")
                row = _counter(value)
                _add(by_role.setdefault(role, _blank()), row)
                key = (pname, None, role, "—")
                _add(rows.setdefault(key, {**_blank(), "project": pname,
                                           "provider": None, "provider_name": None,
                                           "role": role, "model": "—"}), row)
            for model, value in models.items():
                _add(by_model.setdefault(_clean(model or "—"), _blank()), _counter(value))
        else:
            key = (pname, None, "—", "—")
            _add(rows.setdefault(key, {**_blank(), "project": pname,
                                       "provider": None, "provider_name": None,
                                       "role": "—", "model": "—"}), base)
            _add(by_role.setdefault("—", _blank()), base)
            _add(by_model.setdefault("—", _blank()), base)

    def table(src, tokens: bool = False):
        items = []
        for k, v in src.items():
            row = _totals_view(v) if tokens else dict(v)
            items.append(dict(name=k, **row))
        return sorted(items, key=lambda r: (-(r.get("tokens_in", 0) + r.get("tokens_out", 0)),
                                            str(r.get("name") or "")))

    row_list = []
    for value in rows.values():
        row_list.append(_totals_view(value))
    row_list.sort(key=lambda r: (-(r.get("tokens_in", 0) + r.get("tokens_out", 0)),
                                 str(r.get("project") or ""), r.get("provider") or "",
                                 str(r.get("role") or ""), str(r.get("model") or "")))
    return {
        "records": records,
        "old": old,
        "old_schema": old_schema,
        "no_calls": no_calls,
        "since": (utc_stamp(since) if since else ""),
        "totals": {**_totals_view(totals), "estimated": bool(estimated)},
        "projects": table(by_project, tokens=True),
        "providers": table(by_provider, tokens=True),
        "roles": table(by_role, tokens=True),
        "models": table(by_model, tokens=True),
        "tasks": table(by_task, tokens=True),
        "rows": row_list,
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
