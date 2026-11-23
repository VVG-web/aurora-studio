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

import aurora_common as AC

import argparse
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import timing as T
import usage_log as UL

SCHEMA = 2
SOURCE_EXACT = "usage.d"
SOURCE_APPROX = "timing.d"
DEFAULT_LIMIT = 200
API_LIMIT = 100
API_DAYS = (0, 1, 7, 30, 90)
API_KEEP_DAYS = 90
DAY_MIN = date(1970, 1, 1)
DAY_MAX = date(2100, 1, 1)
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
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:160]


_KEYLIKE_RE = re.compile(
    r"(?i)(?:"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"aur_(?:kn|rm)_[A-Za-z0-9_-]{8,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|"
    r"[A-Fa-f0-9]{32,}"
    r")"
)
_TASK_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}(?::[a-z][a-z0-9_.-]{0,63})?",
                      re.IGNORECASE)


def _unsafe_text(text: str) -> bool:
    """URL, абсолютный путь или строка, похожая на ключ, наружу не идут."""
    if "://" in text:
        return True
    if re.search(r"\bbearer\b", text, re.IGNORECASE):
        return True
    if re.match(r"^(?:/|\\|[A-Za-z]:[\\/]|~[\\/])", text):
        return True
    if re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", text):
        return True
    return bool(_KEYLIKE_RE.search(text))


def _safe_label(value, fallback: str = "", keep_relative: bool = False) -> str:
    """Имя для показа без пути и URL: наружу не должна уйти папка или адрес."""
    text = _clean(value)
    if not text or _unsafe_text(text):
        return _clean(fallback)
    if keep_relative:
        return text
    text = text.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return text or _clean(fallback)


def _safe_task(value) -> str:
    """Только имя команды. Свободный текст задачи, путь, URL и ключ не отдаются."""
    text = _clean(value)
    if not text or _unsafe_text(text) or not _TASK_RE.fullmatch(text):
        return ""
    return text.lower()


def _safe_filter(value) -> str:
    """Значение фильтра без URL, абсолютного пути и ключа; иначе пустая строка."""
    text = _clean(value)
    if not text or not re.fullmatch(r"[A-Za-z0-9._:@+-]{1,64}", text) or _unsafe_text(text):
        return ""
    return text


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
    return AC.utc_stamp(float(epoch))


def _day(value) -> str:
    stamp = _epoch(value)
    if stamp is None:
        return ""
    return AC.utc_stamp(stamp)[:10]


def _bucket(value, tz, grain: str) -> str:
    """Корзина ряда в часовом поясе панели: день или час."""
    stamp = _epoch(value)
    if stamp is None:
        return ""
    local = datetime.fromtimestamp(stamp, tz)
    if grain == "hour":
        return local.strftime("%Y-%m-%dT%H:00")
    return local.strftime("%Y-%m-%d")


def _zone(name: str):
    """IANA-пояс из запроса; пусто — пояс машины. Неизвестный пояс — 400."""
    text = str(name or "").strip()
    if not text:
        local = AC.local_now().tzinfo
        return local or timezone.utc, (getattr(local, "key", None) or str(local or timezone.utc))
    try:
        zone = ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("tz: неизвестный часовой пояс") from None
    return zone, text


def _parse_day(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise ValueError(f"{field}: нужен формат YYYY-MM-DD") from None
    if parsed < DAY_MIN or parsed > DAY_MAX:
        raise ValueError(f"{field}: дата вне допустимого диапазона")
    return parsed


def _one(query: dict, key: str, default: str = "") -> str:
    raw = (query or {}).get(key)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else default
    return str(raw if raw is not None else default).strip()


def _many(query: dict, key: str) -> list[str]:
    raw = (query or {}).get(key) or []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def parse_query(query: dict, now: float | None = None) -> dict:
    """Разобрать параметры `/api/usage`. Ошибка параметра — `ValueError` для HTTP 400."""
    now = time.time() if now is None else float(now)
    raw_days = _one(query, "days")
    if raw_days == "":
        days = 7
    else:
        try:
            days = int(raw_days)
        except ValueError:
            raise ValueError("days: нужно целое число") from None
        if days not in API_DAYS:
            raise ValueError("days: допустимо 0, 1, 7, 30 или 90")

    raw_limit = _one(query, "limit")
    if raw_limit == "":
        limit = API_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise ValueError("limit: нужно целое число") from None
        if limit < 1 or limit > API_LIMIT:
            raise ValueError(f"limit: допустимо от 1 до {API_LIMIT}")

    grain = _one(query, "grain") or "day"
    if grain not in ("hour", "day"):
        raise ValueError("grain: допустимо hour или day")
    tz, tz_name = _zone(_one(query, "tz"))
    today = datetime.fromtimestamp(now, tz).date()
    raw_from, raw_to = _one(query, "from"), _one(query, "to")
    to_date = _parse_day(raw_to, "to") if raw_to else today
    if raw_from:
        from_date = _parse_day(raw_from, "from")
    elif days:
        from_date = to_date - timedelta(days=days - 1)
    else:
        from_date = to_date - timedelta(days=API_KEEP_DAYS)
    if from_date > to_date:
        raise ValueError("from: дата начала позже даты конца")

    window_days = 0 if (days == 0 and not raw_from and not raw_to) else \
        (to_date - from_date).days + 1
    start = datetime.combine(from_date, dt_time.min, tzinfo=tz).timestamp()
    end = datetime.combine(to_date + timedelta(days=1), dt_time.min, tzinfo=tz).timestamp()
    keep_start = now - API_KEEP_DAYS * 86400
    if start < keep_start:
        start = keep_start
        from_date = datetime.fromtimestamp(start, tz).date()
        if window_days:
            window_days = (to_date - from_date).days + 1

    filters = {}
    for name in ("project", "model", "provider", "role", "task", "kind"):
        values = []
        for item in _many(query, name):
            value = _safe_filter(item)
            if value:
                values.append(value)
        filters[name] = values
    compare_requested = _one(query, "compare") == "1"
    compare_reason = ""
    if compare_requested and days == 0:
        compare_reason = "days=0"
    elif compare_requested and window_days > 45:
        compare_reason = "days>45"
    compare = compare_requested and not compare_reason
    previous = None
    if compare:
        prev_to = from_date - timedelta(days=1)
        prev_from = prev_to - timedelta(days=max(1, window_days) - 1)
        previous = {
            "from": prev_from.isoformat(), "to": prev_to.isoformat(),
            "start": datetime.combine(prev_from, dt_time.min, tzinfo=tz).timestamp(),
            "end": datetime.combine(prev_to + timedelta(days=1), dt_time.min,
                                    tzinfo=tz).timestamp(),
        }
    return {
        "days": window_days,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "start": start,
        "end": end,
        "tz": tz,
        "tz_name": tz_name,
        "grain": grain,
        "limit": limit,
        "filters": filters,
        "compare": compare,
        "compare_reason": compare_reason,
        "previous": previous,
    }


def _window_form(opts: dict, compare: bool | None = None) -> dict:
    return {
        "from": opts["from"],
        "to": opts["to"],
        "days": opts["days"],
        "tz": opts["tz_name"],
        "grain": opts["grain"],
        "compare": opts["compare"] if compare is None else compare,
    }


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
    if text in meta["aliases"]:
        return meta["slug"]
    return _safe_label(text, meta["slug"])


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
        "project": _safe_label(rec.get("project"), meta["name"]),
        "project_id": _canonical(rec.get("project_id") or rec.get("project"), meta),
        "run": _clean(rec.get("run")),
        "kind": _clean(rec.get("kind")) or "agent",
        "task": _safe_task(rec.get("task")),
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
        "provider": _safe_label(provider, keep_relative=True) or None,
        "provider_name": _safe_label(provider_name, keep_relative=True) or None,
        "role": _safe_label(role, keep_relative=True) or None,
        "model": _safe_label(model, keep_relative=True) or None,
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


def _aggregate_exact(rows: list[dict], tz=timezone.utc, grain: str = "day") -> dict:
    totals = _blank_exact()
    by_project: dict = {}
    by_task: dict = {}
    by_model: dict = {}
    by_provider: dict = {}
    by_role: dict = {}
    series: dict = {}
    spark: dict = {}
    for row in rows:
        totals["runs"] += 1
        _add_counts(totals, row)
        project_key = row["project_id"]
        project = by_project.setdefault(project_key, {
            "source": SOURCE_EXACT, "approximate": False,
            "project": row["project"], "project_id": row["project_id"],
            "runs": 0, **_blank_counts(), "cached_in": None, "reasoning_out": None,
            "last_at": "", "delta_pct": None, "spark": []})
        project["runs"] += 1
        _add_counts(project, row)
        at = str(row.get("at") or row.get("started") or "")
        project["last_at"] = max(str(project.get("last_at") or ""), at)
        task = row["task"] or "-"
        task_row = by_task.setdefault(task, {
            "source": SOURCE_EXACT, "approximate": False, "task": task,
            "runs": 0, **_blank_counts(), "cached_in": None, "reasoning_out": None})
        task_row["runs"] += 1
        _add_counts(task_row, row)
        day_bucket = _bucket(row["started"] or row["at"], tz, "day")
        if day_bucket:
            days = spark.setdefault(project_key, {})
            days[day_bucket] = days.get(day_bucket, 0) + _int(row.get("tokens_total"))
        for cell in _cells(row):
            model = cell["model"] or "-"
            model_row = by_model.setdefault(model, _blank_cell_row(
                SOURCE_EXACT, "model", model, {"last_at": ""}))
            _add_cell(model_row, cell)
            model_row["last_at"] = max(str(model_row.get("last_at") or ""), at)
            provider = cell["provider"] or ""
            provider_row = by_provider.setdefault(provider, _blank_cell_row(
                SOURCE_EXACT, "provider", cell["provider"],
                {"provider_name": cell["provider_name"], "projects": set(),
                 "last_at": ""}))
            _add_cell(provider_row, cell)
            provider_row["last_at"] = max(str(provider_row.get("last_at") or ""), at)
            provider_row["projects"].add(row["project"] or row["project_id"])
            role = cell["role"] or "-"
            role_row = by_role.setdefault(role, _blank_cell_row(
                SOURCE_EXACT, "role", role))
            _add_cell(role_row, cell)
        bucket = _bucket(row["started"] or row["at"], tz, grain)
        if bucket:
            point = series.setdefault(bucket, {
                "source": SOURCE_EXACT, "approximate": False,
                "bucket": bucket, **({"day": bucket} if grain == "day" else {"hour": bucket}),
                "runs": 0, **_blank_counts(), "tokens_total": 0})
            point["runs"] += 1
            _add_counts(point, row)
            point["tokens_total"] = point["tokens_in"] + point["tokens_out"]
    _finish_counts(totals)
    projects = [_finish_counts(row) for row in by_project.values()]
    tasks = [_finish_counts(row) for row in by_task.values()]
    for row in projects:
        buckets = spark.get(row["project_id"], {})
        row["spark"] = [buckets[key] for key in sorted(buckets)][-14:]
    projects.sort(key=lambda r: (-_int(r.get("tokens_total")),
                                 str(r.get("project_id") or "")))
    tasks.sort(key=lambda r: (-_int(r.get("tokens_total")), str(r.get("task") or "")))
    models = _finish_cell_table(list(by_model.values()), totals["tokens_total"])
    providers = _finish_cell_table(list(by_provider.values()), totals["tokens_total"])
    for row in providers:
        row["projects"] = sorted(str(name) for name in row.get("projects") or [])
    return {
        "totals": totals,
        "by_project": projects,
        "by_model": models,
        "by_provider": providers,
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
                "project": _safe_label(rec.get("project"), meta["name"]),
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
                row["task"] = _safe_task(rec.get("task"))
            if str(rec.get("at") or "") > str(row.get("at") or ""):
                row["at"] = str(rec.get("at") or "")
    shown, suppressed = [], 0
    for key, row in groups.items():
        if (row["project_id"], row["run"]) in exact_keys:
            suppressed += 1
            continue
        shown.append(row)
    return shown, suppressed, timing_seen


def _aggregate_approx(rows: list[dict], tz=timezone.utc, grain: str = "day") -> dict:
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
        bucket = _bucket(row["at"], tz, grain)
        if bucket:
            point = series.setdefault(bucket, {
                "source": SOURCE_APPROX, "approximate": True,
                "bucket": bucket, **({"day": bucket} if grain == "day" else {"hour": bucket}),
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


def _trim_series(rows: list[dict], limit: int = 90) -> tuple[list[dict], bool]:
    """Ряд не длиннее 90 точек; оставляем последние, чтобы не терять свежие дни."""
    if len(rows) > limit:
        return rows[-limit:], True
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


def _scan_bad(projects) -> int:
    """Сколько файлов `usage.d` не читаются как JSON. Ошибка одного файла не прячет другие."""
    bad = 0
    for meta in _projects(projects):
        folder = meta["path"] / "AuroraKnowledgeDB" / "meta" / "usage.d"
        if not folder.is_dir():
            continue
        try:
            files = list(folder.glob("*/*.json"))
        except OSError:
            continue
        for path in files:
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                bad += 1
    return bad


def _engine_old(projects) -> bool:
    """Проект без `usage.d` и без `usage_log.py` — движок старше учёта токенов."""
    for meta in _projects(projects):
        root = meta["path"]
        if (root / "AuroraKnowledgeDB" / "meta" / "usage.d").is_dir():
            continue
        scripts = root / ".aurora" / "scripts"
        if scripts.is_dir() and not (scripts / "usage_log.py").is_file():
            return True
    return False


def _within(rows: list[dict], start: float, end: float) -> list[dict]:
    out = []
    for row in rows:
        stamp = _epoch(row.get("at") or row.get("started"))
        if stamp is not None and start <= stamp < end:
            out.append(row)
    return out


def _set(values) -> set[str]:
    return {str(value).strip().casefold() for value in values or [] if str(value).strip()}


def _cell_matches(cell: dict, filters: dict) -> bool:
    for name in ("provider", "model", "role"):
        wanted = _set(filters.get(name))
        if wanted and str(cell.get(name) or "").casefold() not in wanted:
            return False
    return True


def _row_from_cells(row: dict, cells: list[dict]) -> dict:
    """Строка, у которой итог считается только по выбранным ячейкам."""
    out = dict(row)
    out["by_cell"] = cells
    out["by_pair"] = {}
    out["by_role"] = {}
    out["by_model"] = {}
    out["calls"] = sum(_int(cell.get("calls")) for cell in cells)
    out["calls_failed"] = sum(_int(cell.get("calls_failed")) for cell in cells)
    out["requests"] = sum(_int(cell.get("requests")) for cell in cells)
    out["tokens_in"] = sum(_int(cell.get("tokens_in")) for cell in cells)
    out["tokens_out"] = sum(_int(cell.get("tokens_out")) for cell in cells)
    out["gen_seconds"] = round(sum(_float(cell.get("gen_seconds")) for cell in cells), 2)
    cached = [cell.get("cached_in") for cell in cells if cell.get("cached_in") is not None]
    reasoning = [cell.get("reasoning_out") for cell in cells
                 if cell.get("reasoning_out") is not None]
    out["cached_in"] = sum(_int(value) for value in cached) if cached else None
    out["reasoning_out"] = sum(_int(value) for value in reasoning) if reasoning else None
    out["tokens_total"] = out["tokens_in"] + out["tokens_out"]
    out["has_breakdown"] = True
    return out


def _filter_exact(rows: list[dict], filters: dict) -> list[dict]:
    projects = _set(filters.get("project"))
    tasks = _set(filters.get("task"))
    kinds = _set(filters.get("kind"))
    cell_filter = any(filters.get(name) for name in ("provider", "model", "role"))
    out = []
    for row in rows:
        if projects and row["project_id"].casefold() not in projects \
                and row["project"].casefold() not in projects:
            continue
        if tasks and str(row.get("task") or "").casefold() not in tasks:
            continue
        if kinds and str(row.get("kind") or "").casefold() not in kinds:
            continue
        if cell_filter:
            cells = [cell for cell in _cells(row) if _cell_matches(cell, filters)]
            if not cells:
                continue
            row = _row_from_cells(row, cells)
        out.append(row)
    return out


def _filter_approx(rows: list[dict], filters: dict) -> list[dict]:
    if any(filters.get(name) for name in ("provider", "model", "role")):
        return []
    projects = _set(filters.get("project"))
    tasks = _set(filters.get("task"))
    kinds = _set(filters.get("kind"))
    out = []
    for row in rows:
        if projects and row["project_id"].casefold() not in projects \
                and row["project"].casefold() not in projects:
            continue
        if tasks and str(row.get("task") or "").casefold() not in tasks:
            continue
        if kinds and str(row.get("kind") or "").casefold() not in kinds:
            continue
        out.append(row)
    return out


def _api_quality(exact_rows: list[dict], raw: int, old_schema: int,
                 approx_rows: list[dict], suppressed: int, timing_seen: bool,
                 bad: int, filtered: bool, engine_old: bool) -> dict:
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
        if filtered and (raw or timing_seen):
            empty_reason = "filter"
        elif engine_old and not raw and not timing_seen:
            empty_reason = "engine_old"
        elif raw and no_calls == raw and not approx_rows:
            empty_reason = "no_calls"
        else:
            empty_reason = "no_records"
    elif exact_rows and not any(row["calls"] for row in exact_rows) and not approx_rows:
        empty_reason = "no_calls"
    elif exact_rows and no_breakdown == len(exact_rows) and not approx_rows:
        empty_reason = "all_old"
    else:
        empty_reason = ""
    return {
        "records": raw,
        "records_bad": bad,
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


def _read_api_window(projects, start: float, end: float, filters: dict):
    exact_all, raw, old_schema = _read_exact(projects, since=start)
    exact_rows = _filter_exact(_within(exact_all, start, end), filters)
    exact_keys = {(row["project_id"], row["run"]) for row in exact_rows}
    approx_all, suppressed, timing_seen = _read_approx(projects, start, exact_keys)
    approx_rows = _filter_approx(_within(approx_all, start, end), filters)
    return {
        "exact": exact_rows, "raw": raw, "old_schema": old_schema,
        "approx": approx_rows, "suppressed": suppressed, "timing_seen": timing_seen,
    }


def empty_report(reason: str, error: str = "") -> dict:
    """Пустой ответ, когда модуль чтения не установлен или источник недоступен."""
    totals = _finish_counts(_blank_exact())
    approx = _blank_approx()
    return {
        "schema": SCHEMA,
        "window": {"from": "", "to": "", "days": 0, "tz": "UTC",
                   "grain": "day", "compare": False},
        "totals": totals, "approx": approx, "previous": None,
        "series": [], "approx_series": [],
        "by_project": [], "by_model": [], "by_provider": [], "by_role": [],
        "by_task": [], "approx_by_project": [],
        "quality": {"records": 0, "records_bad": 0, "records_no_calls": 0,
                    "records_no_breakdown": 0, "records_unknown_provider": 0,
                    "records_unknown_usage": 0, "records_old_schema": 0,
                    "approx_records": 0, "approx_tokens": 0,
                    "approx_suppressed_by_exact": 0, "sources": [],
                    "cached_known": False, "reasoning_known": False,
                    "empty": True, "empty_reason": reason,
                    "truncated": False, "error": error},
        "filters": {}, "options": {},
    }


def query(projects, params: dict, now: float | None = None,
          key_present: dict | None = None) -> dict:
    """Ответ `/api/usage`: окно, фильтры, точный и приблизительный блоки."""
    opts = parse_query(params, now)
    got = _read_api_window(projects, opts["start"], opts["end"], opts["filters"])
    exact = _aggregate_exact(got["exact"], tz=opts["tz"], grain=opts["grain"])
    approx = _aggregate_approx(got["approx"], tz=opts["tz"], grain=opts["grain"])
    quality = _api_quality(
        got["exact"], got["raw"], got["old_schema"], got["approx"],
        got["suppressed"], got["timing_seen"], _scan_bad(projects),
        any(opts["filters"].values()), _engine_old(projects))
    limit = opts["limit"]
    trimmed = False
    tables = {}
    for name in ("by_project", "by_model", "by_provider", "by_role", "by_task"):
        tables[name], cut = _trim(exact[name], limit)
        trimmed = trimmed or cut
    approx_projects, cut = _trim(approx["approx_by_project"], limit)
    trimmed = trimmed or cut
    series, cut = _trim_series(exact["series"])
    trimmed = trimmed or cut
    approx_series, cut = _trim_series(approx["approx_series"])
    trimmed = trimmed or cut
    quality["truncated"] = trimmed
    presence = key_present or {}
    for row in tables["by_provider"]:
        provider = row.get("provider")
        row["key_present"] = bool(presence[provider]) if provider in presence else None

    previous = None
    if opts["compare"]:
        prev = _read_api_window(projects, opts["previous"]["start"],
                                opts["previous"]["end"], opts["filters"])
        prev_exact = _aggregate_exact(prev["exact"], tz=opts["tz"], grain=opts["grain"])
        prev_approx = _aggregate_approx(prev["approx"], tz=opts["tz"], grain=opts["grain"])
        previous_projects = {row["project_id"]: _int(row.get("tokens_total"))
                             for row in prev_exact["by_project"]}
        for row in tables["by_project"]:
            before = previous_projects.get(row["project_id"], 0)
            row["delta_pct"] = (round((_int(row.get("tokens_total")) - before)
                                      / before * 100, 1) if before else None)
        previous = {
            "window": {"from": opts["previous"]["from"], "to": opts["previous"]["to"],
                       "days": opts["days"], "tz": opts["tz_name"],
                       "grain": opts["grain"]},
            "totals": prev_exact["totals"], "approx": prev_approx["approx"],
        }
    elif opts["compare_reason"]:
        previous = {"reason": opts["compare_reason"]}

    return {
        "schema": SCHEMA,
        "window": _window_form(opts),
        "totals": exact["totals"],
        "approx": approx["approx"],
        "previous": previous,
        "series": series,
        "approx_series": approx_series,
        "by_project": tables["by_project"],
        "by_model": tables["by_model"],
        "by_provider": tables["by_provider"],
        "by_role": tables["by_role"],
        "by_task": tables["by_task"],
        "approx_by_project": approx_projects,
        "quality": quality,
        "filters": {name: list(values) for name, values in opts["filters"].items()},
        "options": {},
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
