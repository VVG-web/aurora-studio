#!/usr/bin/env python3
"""usage_metrics.py — точный и приблизительный расход токенов для «Замеров».

Точный источник один: `AuroraKnowledgeDB/meta/usage.d`, его читает
`usage_log.iter_records`. Приблизительный источник — `timing.d`, его читает
`timing.iter_records`. `timing.jsonl` не читается: он живёт только последние
50 прогонов и не переживает окно дашборда.

Из `timing.d` берётся только `services.llm`: `calls`, `tokens_in`, `tokens_out`.
Эмбеддинги, OCR, веб и остальные сервисы в токены LLM не попадают: их считает
свой разрез, и складывать их с ответами модели нельзя.

Правила сведения:

- точная запись всегда побеждает приблизительную для того же
  `(project_id, run)`;
- приблизительные записи одного прогона и одного вида суммируются по
  `(project_id, run, kind)`;
- приблизительный блок никогда не прибавляется к точному итогу;
- несколько точных записей одного прогона не суммируются, побеждает более
  новый `at`;
- `record_id` нужен только для дедупликации внутри `timing.iter_records`.

Модуль ничего не пишет. Ошибка чтения одного источника не прячет другой.

  python3 scripts/usage_metrics.py --project . --days 7
  python3 scripts/usage_metrics.py --project . --days 7 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import timing as T
import usage_log as UL

SCHEMA = 2
SOURCE_EXACT = "usage.d"
SOURCE_APPROX = "timing.d"
DEFAULT_LIMIT = 200
_COUNT_KEYS = ("calls", "calls_failed", "requests", "tokens_in", "tokens_out",
               "tokens_failed_in", "tokens_failed_out", "cache_hits", "gen_seconds")
_NULL_KEYS = ("cached_in", "reasoning_out")


def _int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _nullable_int(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:160]


def _epoch(value):
    """ISO-время в секунды эпохи; пусто или мусор → None."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(epoch) -> str:
    if epoch in (None, ""):
        return ""
    return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(value) -> str:
    stamp = _epoch(value)
    if stamp is None:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")


def _config_slug(root: Path) -> str:
    try:
        text = (root / "aurora.config.yaml").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    try:
        from aurora_common import yaml_scalar
        return _clean(yaml_scalar(text, "slug"))
    except Exception:  # noqa: BLE001 — без конфига проект остаётся именем папки
        return ""


def _projects(projects) -> list[dict]:
    """Корни проектов с именем, slug и алиасами для сведения источников."""
    if isinstance(projects, (str, os.PathLike)):
        projects = [projects]
    out, seen = [], set()
    for project in projects or []:
        if isinstance(project, dict):
            path = project.get("path") or project.get("id") or ""
        else:
            path = project
        if not path:
            continue
        root = Path(str(path)).expanduser().resolve()
        if str(root) in seen:
            continue
        seen.add(str(root))
        name = _clean(project.get("name")) if isinstance(project, dict) else ""
        name = name or root.name
        slug = _clean(project.get("slug")) if isinstance(project, dict) else ""
        slug = slug or _config_slug(root) or name
        aliases = {name, slug, root.name, str(root)}
        out.append({"path": root, "name": name, "slug": slug, "aliases": aliases})
    return out


def _canonical(value, meta: dict) -> str:
    text = _clean(value)
    return meta["slug"] if text in meta["aliases"] else (text or meta["slug"])


def _blank_counts() -> dict:
    row = {key: 0 for key in _COUNT_KEYS}
    row["gen_seconds"] = 0.0
    return row


def _blank_exact() -> dict:
    row = {"source": SOURCE_EXACT, "approximate": False, "runs": 0,
           **_blank_counts(), "cached_in": None, "reasoning_out": None}
    return row


def _blank_approx() -> dict:
    return {"source": SOURCE_APPROX, "approximate": True, "runs": 0,
            "calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_total": 0}


def _add_counts(dst: dict, row: dict) -> None:
    for key in _COUNT_KEYS:
        if key == "gen_seconds":
            dst[key] = round(_float(dst.get(key)) + _float(row.get(key)), 2)
        else:
            dst[key] = _int(dst.get(key)) + _int(row.get(key))
    for key in _NULL_KEYS:
        value = _nullable_int(row.get(key))
        if value is None:
            continue
        dst[key] = _int(dst.get(key)) + value


def _finish_counts(row: dict) -> dict:
    row["tokens_total"] = _int(row.get("tokens_in")) + _int(row.get("tokens_out"))
    row["tokens_failed"] = (_int(row.get("tokens_failed_in"))
                            + _int(row.get("tokens_failed_out")))
    row["tokens_all"] = row["tokens_total"] + row["tokens_failed"]
    calls, runs = _int(row.get("calls")), _int(row.get("runs"))
    row["tokens_per_call"] = (round(row["tokens_total"] / calls, 2) if calls else None)
    row["tokens_per_run"] = (round(row["tokens_total"] / runs, 2) if runs else None)
    return row


def _exact_row(rec: dict, meta: dict) -> dict:
    row = {
        "_schema": _int(rec.get("schema"), 1) or 1,
        "source": SOURCE_EXACT, "approximate": False,
        "project": _clean(rec.get("project")) or meta["name"],
        "project_id": _canonical(rec.get("project_id") or rec.get("project"), meta),
        "run": _clean(rec.get("run")),
        "kind": _clean(rec.get("kind")) or "agent",
        "task": _clean(rec.get("task")),
        "at": str(rec.get("at") or ""),
        "started": str(rec.get("started") or ""),
        "seconds": round(_float(rec.get("seconds")), 2),
        "calls": _int(rec.get("calls")),
        "calls_failed": _int(rec.get("calls_failed")),
        "requests": _int(rec.get("requests")),
        "tokens_in": _int(rec.get("tokens_in")),
        "tokens_out": _int(rec.get("tokens_out")),
        "tokens_failed_in": _int(rec.get("tokens_failed_in")),
        "tokens_failed_out": _int(rec.get("tokens_failed_out")),
        "cached_in": _nullable_int(rec.get("cached_in")),
        "reasoning_out": _nullable_int(rec.get("reasoning_out")),
        "cache_hits": _int(rec.get("cache_hits")),
        "usage_missing_calls": _int(rec.get("usage_missing_calls")),
        "gen_seconds": round(_float(rec.get("gen_seconds")), 2),
        "estimated": bool(rec.get("estimated")),
        "errors": _int(rec.get("errors")),
        "by_cell": rec.get("by_cell") if isinstance(rec.get("by_cell"), list) else [],
        "by_role": rec.get("by_role") if isinstance(rec.get("by_role"), dict) else {},
        "by_model": rec.get("by_model") if isinstance(rec.get("by_model"), dict) else {},
        "by_pair": rec.get("by_pair") if isinstance(rec.get("by_pair"), dict) else {},
    }
    row["tokens_total"] = row["tokens_in"] + row["tokens_out"]
    row["has_breakdown"] = bool(row["by_cell"] or row["by_pair"]
                                or row["by_role"] or row["by_model"])
    return row


def _read_exact(projects, since: float = 0.0):
    """Точные записи, без повторов по `(project_id, run)`. → (rows, raw, old_schema)."""
    rows, raw, old_schema = [], 0, 0
    for meta in _projects(projects):
        for rec in UL.iter_records(str(meta["path"]), since=since):
            stamp = _epoch(rec.get("at"))
            if since and stamp is not None and stamp < since:
                continue
            row = _exact_row(rec, meta)
            if not row["run"]:
                continue
            raw += 1
            if row["_schema"] != 2:
                old_schema += 1
            rows.append(row)
    winners: dict = {}
    for row in rows:
        key = (row["project_id"], row["run"])
        prev = winners.get(key)
        if prev is None or str(row.get("at") or "") >= str(prev.get("at") or ""):
            winners[key] = row
    return list(winners.values()), raw, old_schema


def _cell(provider=None, provider_name=None, role=None, model=None, calls=0,
          calls_failed=0, requests=0, tokens_in=0, tokens_out=0, cached_in=None,
          reasoning_out=None, gen_seconds=0.0) -> dict:
    row = {
        "provider": _clean(provider) or None,
        "provider_name": _clean(provider_name) or None,
        "role": _clean(role) or None,
        "model": _clean(model) or None,
        "calls": _int(calls), "calls_failed": _int(calls_failed),
        "requests": _int(requests),
        "tokens_in": _int(tokens_in), "tokens_out": _int(tokens_out),
        "cached_in": _nullable_int(cached_in),
        "reasoning_out": _nullable_int(reasoning_out),
        "gen_seconds": round(_float(gen_seconds), 2),
    }
    row["tokens_total"] = row["tokens_in"] + row["tokens_out"]
    return row


def _cells(row: dict) -> list[dict]:
    """Ячейки точной записи: by_cell, затем by_pair, затем одна неизвестная."""
    cells = []
    raw = row.get("by_cell") or []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            cells.append(_cell(
                provider=item.get("provider"), provider_name=item.get("provider_name"),
                role=item.get("role"), model=item.get("model"),
                calls=item.get("calls"), calls_failed=item.get("calls_failed"),
                requests=item.get("requests"), tokens_in=item.get("tokens_in"),
                tokens_out=item.get("tokens_out"), cached_in=item.get("cached_in"),
                reasoning_out=item.get("reasoning_out"),
                gen_seconds=item.get("gen_seconds")))
    if cells:
        return cells
    pairs = row.get("by_pair") or {}
    if isinstance(pairs, dict):
        for key, value in pairs.items():
            if not isinstance(value, dict):
                continue
            role, _sep, model = str(key).partition("|")
            cells.append(_cell(
                role=role, model=model, calls=value.get("calls"),
                calls_failed=value.get("calls_failed"), requests=value.get("requests"),
                tokens_in=value.get("tokens_in"), tokens_out=value.get("tokens_out"),
                cached_in=value.get("cached_in"),
                reasoning_out=value.get("reasoning_out"),
                gen_seconds=value.get("gen_seconds") or value.get("seconds")))
    if cells:
        return cells
    # Старая запись без разбивки: одна неизвестная ячейка, полный итог не размножается.
    return [_cell(calls=row.get("calls"), calls_failed=row.get("calls_failed"),
                  requests=row.get("requests"), tokens_in=row.get("tokens_in"),
                  tokens_out=row.get("tokens_out"), cached_in=row.get("cached_in"),
                  reasoning_out=row.get("reasoning_out"),
                  gen_seconds=row.get("gen_seconds"))]


def _add_cell(dst: dict, cell: dict) -> None:
    for key in ("calls", "calls_failed", "requests", "tokens_in", "tokens_out"):
        dst[key] = _int(dst.get(key)) + _int(cell.get(key))
    dst["gen_seconds"] = round(_float(dst.get("gen_seconds"))
                               + _float(cell.get("gen_seconds")), 2)
    for key in _NULL_KEYS:
        value = _nullable_int(cell.get(key))
        if value is not None:
            dst[key] = _int(dst.get(key)) + value


def _blank_cell_row(source: str, label: str, label_value, extra: dict | None = None) -> dict:
    row = {"source": source, "approximate": source == SOURCE_APPROX,
           label: label_value, "calls": 0, "calls_failed": 0, "requests": 0,
           "tokens_in": 0, "tokens_out": 0, "cached_in": None,
           "reasoning_out": None, "gen_seconds": 0.0}
    if extra:
        row.update(extra)
    return row


def _finish_cell_table(rows: list[dict], total_tokens: int) -> list[dict]:
    for row in rows:
        row["tokens_total"] = _int(row.get("tokens_in")) + _int(row.get("tokens_out"))
        row["share"] = (round(row["tokens_total"] / total_tokens, 4)
                        if total_tokens else 0.0)
    rows.sort(key=lambda r: (-_int(r.get("tokens_total")),
                             str(r.get("project_id") or r.get("name") or "")))
    return rows


def _aggregate_exact(rows: list[dict]) -> dict:
    totals = _blank_exact()
    by_project: dict = {}
    by_task: dict = {}
    by_model: dict = {}
    by_provider: dict = {}
    by_role: dict = {}
    series: dict = {}
    for row in rows:
        totals["runs"] += 1
        _add_counts(totals, row)
        project_key = row["project_id"]
        project = by_project.setdefault(project_key, {
            "source": SOURCE_EXACT, "approximate": False,
            "project": row["project"], "project_id": row["project_id"],
            "runs": 0, **_blank_counts(), "cached_in": None, "reasoning_out": None})
        project["runs"] += 1
        _add_counts(project, row)
        task = row["task"] or "-"
        task_row = by_task.setdefault(task, {
            "source": SOURCE_EXACT, "approximate": False, "task": task,
            "runs": 0, **_blank_counts(), "cached_in": None, "reasoning_out": None})
        task_row["runs"] += 1
        _add_counts(task_row, row)
        for cell in _cells(row):
            model = cell["model"] or "-"
            model_row = by_model.setdefault(model, _blank_cell_row(
                SOURCE_EXACT, "model", model))
            _add_cell(model_row, cell)
            provider = cell["provider"] or ""
            provider_row = by_provider.setdefault(provider, _blank_cell_row(
                SOURCE_EXACT, "provider", cell["provider"],
                {"provider_name": cell["provider_name"]}))
            _add_cell(provider_row, cell)
            role = cell["role"] or "-"
            role_row = by_role.setdefault(role, _blank_cell_row(
                SOURCE_EXACT, "role", role))
            _add_cell(role_row, cell)
        day = _day(row["started"] or row["at"])
        if day:
            point = series.setdefault(day, {
                "source": SOURCE_EXACT, "approximate": False, "day": day,
                "runs": 0, **_blank_counts(), "tokens_total": 0})
            point["runs"] += 1
            _add_counts(point, row)
            point["tokens_total"] = point["tokens_in"] + point["tokens_out"]
    _finish_counts(totals)
    projects = [_finish_counts(row) for row in by_project.values()]
    tasks = [_finish_counts(row) for row in by_task.values()]
    projects.sort(key=lambda r: (-_int(r.get("tokens_total")),
                                 str(r.get("project_id") or "")))
    tasks.sort(key=lambda r: (-_int(r.get("tokens_total")), str(r.get("task") or "")))
    return {
        "totals": totals,
        "by_project": projects,
        "by_model": _finish_cell_table(list(by_model.values()), totals["tokens_total"]),
        "by_provider": _finish_cell_table(list(by_provider.values()), totals["tokens_total"]),
        "by_role": _finish_cell_table(list(by_role.values()), totals["tokens_total"]),
        "by_task": tasks,
        "series": [series[key] for key in sorted(series)],
    }


def _read_approx(projects, since: float, exact_keys: set):
    """Приблизительные записи из timing.d. → (rows, suppressed, timing_seen)."""
    groups: dict = {}
    timing_seen = False
    for meta in _projects(projects):
        for rec in T.iter_records([str(meta["path"])], since=since, limit=0):
            services = rec.get("services")
            llm = services.get("llm") if isinstance(services, dict) else None
            if not isinstance(llm, dict):
                continue
            calls = _int(llm.get("calls"))
            tokens_in = _int(llm.get("tokens_in"))
            tokens_out = _int(llm.get("tokens_out"))
            if not (calls or tokens_in or tokens_out):
                continue
            timing_seen = True
            project_id = _canonical(rec.get("project") or meta["name"], meta)
            run = _clean(rec.get("run"))
            if not run:
                continue
            kind = _clean(rec.get("kind")) or "job"
            key = (project_id, run, kind)
            row = groups.setdefault(key, {
                "source": SOURCE_APPROX, "approximate": True,
                "project": _clean(rec.get("project")) or meta["name"],
                "project_id": project_id, "run": run, "kind": kind,
                "task": "", "at": "",
                "calls": 0, "tokens_in": 0, "tokens_out": 0, "tokens_total": 0,
                "provider": None, "model": None, "role": None,
            })
            row["calls"] += calls
            row["tokens_in"] += tokens_in
            row["tokens_out"] += tokens_out
            row["tokens_total"] = row["tokens_in"] + row["tokens_out"]
            if rec.get("task"):
                row["task"] = _clean(rec.get("task"))
            if str(rec.get("at") or "") > str(row.get("at") or ""):
                row["at"] = str(rec.get("at") or "")
    shown, suppressed = [], 0
    for key, row in groups.items():
        if (row["project_id"], row["run"]) in exact_keys:
            suppressed += 1
            continue
        shown.append(row)
    return shown, suppressed, timing_seen


def _aggregate_approx(rows: list[dict]) -> dict:
    totals = _blank_approx()
    by_project: dict = {}
    series: dict = {}
    for row in rows:
        totals["runs"] += 1
        totals["calls"] += row["calls"]
        totals["tokens_in"] += row["tokens_in"]
        totals["tokens_out"] += row["tokens_out"]
        key = row["project_id"]
        project = by_project.setdefault(key, {
            "source": SOURCE_APPROX, "approximate": True,
            "project": row["project"], "project_id": row["project_id"],
            "runs": 0, "calls": 0, "tokens_in": 0, "tokens_out": 0,
            "tokens_total": 0, "provider": None, "model": None, "role": None})
        project["runs"] += 1
        project["calls"] += row["calls"]
        project["tokens_in"] += row["tokens_in"]
        project["tokens_out"] += row["tokens_out"]
        project["tokens_total"] = project["tokens_in"] + project["tokens_out"]
        day = _day(row["at"])
        if day:
            point = series.setdefault(day, {
                "source": SOURCE_APPROX, "approximate": True, "day": day,
                "runs": 0, "calls": 0, "tokens_in": 0, "tokens_out": 0,
                "tokens_total": 0})
            point["runs"] += 1
            point["calls"] += row["calls"]
            point["tokens_in"] += row["tokens_in"]
            point["tokens_out"] += row["tokens_out"]
            point["tokens_total"] = point["tokens_in"] + point["tokens_out"]
    totals["tokens_total"] = totals["tokens_in"] + totals["tokens_out"]
    projects = sorted(by_project.values(), key=lambda r: (
        -_int(r.get("tokens_total")), str(r.get("project_id") or "")))
    for row in projects:
        row["share"] = (round(row["tokens_total"] / totals["tokens_total"], 4)
                        if totals["tokens_total"] else 0.0)
    return {"approx": totals, "approx_by_project": projects,
            "approx_series": [series[key] for key in sorted(series)]}


def _trim(rows: list[dict], limit: int) -> tuple[list[dict], bool]:
    if limit and len(rows) > limit:
        return rows[:limit], True
    return rows, False


def _quality(exact_rows: list[dict], raw: int, old_schema: int, approx_rows: list[dict],
             suppressed: int, timing_seen: bool) -> dict:
    no_calls = sum(1 for row in exact_rows if not row["calls"])
    no_breakdown = sum(1 for row in exact_rows if not row.get("has_breakdown"))
    unknown_provider = sum(1 for row in exact_rows
                           if all(cell.get("provider") is None for cell in _cells(row)))
    unknown_usage = sum(1 for row in exact_rows if row.get("usage_missing_calls"))
    approx_tokens = sum(row["tokens_total"] for row in approx_rows)
    sources = []
    if raw:
        sources.append(SOURCE_EXACT)
    if timing_seen:
        sources.append(SOURCE_APPROX)
    empty = not exact_rows and not approx_rows
    if empty:
        empty_reason = "no_calls" if raw and no_calls == len(exact_rows) else "no_records"
    elif exact_rows and not any(row["calls"] for row in exact_rows) and not approx_rows:
        empty_reason = "no_calls"
    else:
        empty_reason = ""
    return {
        "records": raw,
        "records_bad": 0,
        "records_no_calls": no_calls,
        "records_no_breakdown": no_breakdown,
        "records_unknown_provider": unknown_provider,
        "records_unknown_usage": unknown_usage,
        "records_old_schema": old_schema,
        "approx_records": len(approx_rows),
        "approx_tokens": approx_tokens,
        "approx_suppressed_by_exact": suppressed,
        "sources": sources,
        "cached_known": any(row.get("cached_in") is not None for row in exact_rows),
        "reasoning_known": any(row.get("reasoning_out") is not None for row in exact_rows),
        "empty": empty,
        "empty_reason": empty_reason,
        "truncated": False,
        "error": "",
    }


def collect(projects, days: int = 7, now: float | None = None,
            limit: int = DEFAULT_LIMIT) -> dict:
    """Сводка точных и приблизительных токенов. Ничего не пишет.

    `days=0` значит «без окна»; предел 90 дней проверяет вызывающий (API).
    `limit` обрезает таблицы, но не итоги.
    """
    now = time.time() if now is None else float(now)
    days = max(0, _int(days))
    since = (now - days * 86400) if days else 0.0
    exact_rows, raw, old_schema = _read_exact(projects, since=since)
    exact_keys = {(row["project_id"], row["run"]) for row in exact_rows}
    approx_rows, suppressed, timing_seen = _read_approx(projects, since, exact_keys)
    exact = _aggregate_exact(exact_rows)
    approx = _aggregate_approx(approx_rows)
    quality = _quality(exact_rows, raw, old_schema, approx_rows, suppressed, timing_seen)
    limit = max(0, _int(limit))
    trimmed = False
    exact_tables = {}
    for key in ("by_project", "by_model", "by_provider", "by_role", "by_task"):
        exact_tables[key], cut = _trim(exact[key], limit)
        trimmed = trimmed or cut
    approx["approx_by_project"], cut = _trim(approx["approx_by_project"], limit)
    trimmed = trimmed or cut
    quality["truncated"] = trimmed
    return {
        "schema": SCHEMA,
        "window": {"days": days, "since": _iso(since) if since else "",
                   "until": _iso(now)},
        "totals": exact["totals"],
        "approx": approx["approx"],
        "series": exact["series"],
        "approx_series": approx["approx_series"],
        "by_project": exact_tables["by_project"],
        "by_model": exact_tables["by_model"],
        "by_provider": exact_tables["by_provider"],
        "by_role": exact_tables["by_role"],
        "by_task": exact_tables["by_task"],
        "approx_by_project": approx["approx_by_project"],
        "quality": quality,
    }


def summary(projects, days: int = 7, now: float | None = None,
            limit: int = DEFAULT_LIMIT) -> dict:
    """Совместимое имя для API: то же, что `collect`."""
    return collect(projects, days=days, now=now, limit=limit)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="usage_metrics.py",
                                 description="Точные и приблизительные токены «Замеров».")
    ap.add_argument("--project", action="append", default=[], help="корень проекта (можно несколько)")
    ap.add_argument("--days", type=int, default=7, help="0 — без окна")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="строк в таблице")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    got = collect(args.project, days=args.days, limit=args.limit)
    if args.json:
        print(json.dumps(got, ensure_ascii=False, indent=1))
    else:
        t, a, q = got["totals"], got["approx"], got["quality"]
        print(f"точно: прогонов {t['runs']}, токенов {t['tokens_total']}")
        print(f"оценка timing.d: прогонов {a['runs']}, токенов {a['tokens_total']} "
              f"· подавлено точными {q['approx_suppressed_by_exact']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
