#!/usr/bin/env python3
"""net_report.py — почему прогон шёл долго: сеть, VPN, провайдеры или локальная работа.

Читает снимки `net_observe` из архива прогона или из общего файла участника.
Сеть не опрашивает, модель не зовёт, ничего не пишет.

Панель: `net:report`
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aurora_common  # noqa: F401,E402 — защита версии Python

import net_observe as NO  # noqa: E402
import timing as T  # noqa: E402


def _time(seconds) -> str:
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, sec = divmod(rest, 60)
    return (f"{hours} ч {minutes:02d} мин" if hours else
            f"{minutes} мин {sec:02d} с" if minutes else f"{sec} с")


def _meta(project: str, run_id: str) -> dict:
    """`meta.json` прогона рядом со снимками: в нём очередь и точная сводка."""
    if not project or not run_id:
        return {}
    try:
        from aurora_common import engine_dir
        path = Path(project).expanduser().resolve() / engine_dir(project) / "runs" / run_id / "meta.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _net_summary(meta: dict, summary: dict | None) -> dict:
    """Снимки сети: сводка отчёта, иначе `meta.net`; пусто без снимков."""
    for candidate in (summary, meta.get("net")):
        if not isinstance(candidate, dict):
            continue
        inner = candidate.get("summary")
        row = inner if isinstance(inner, dict) else candidate
        try:
            snapshots = int(row.get("snapshots") or 0)
        except (TypeError, ValueError):
            snapshots = 0
        if snapshots > 0:
            return row
    return {}


def _timing_of(meta: dict, seconds, summary: dict) -> dict:
    timing = meta.get("timing") if isinstance(meta.get("timing"), dict) else None
    if timing:
        return timing
    if seconds is None:
        seconds = meta.get("seconds")
    return T.legacy_timing(seconds, _net_summary(meta, summary) or None,
                           queue_ms=meta.get("queue_ms"))


def _show(data: dict, project: str = "") -> str:
    summary = data.get("summary") or NO.summarize(data)
    # Пустой отчёт не получает `seconds` из `summarize`: там ноль, а не измеренная
    # длительность. Передаём None, чтобы строка сказала «нет данных», а не «0 с».
    seconds = (data.get("summary") or {}).get("seconds")
    if seconds is None:
        seconds = data.get("seconds")
    if seconds is None and summary.get("snapshots"):
        seconds = summary.get("seconds")
    meta = _meta(project, str(data.get("id") or ""))
    timing = _timing_of(meta, seconds, summary)
    lines = [f"# Сеть прогона {data.get('id', '')}", "",
             f"- команда: {data.get('cmd') or '—'}",
             f"- длительность: {T.format_work_wait(timing)}",
             f"- снимков: {summary.get('snapshots', 0)}"
             + (f" · долгих: {sum(1 for s in data.get('snapshots') or [] if s.get('scope') == 'long')}"
                if data.get("snapshots") else ""),
             f"- сеть нужна: {'да' if summary.get('network_required') else 'нет'}",
             f"- вывод: {summary.get('reason') or '—'}"]
    if (timing.get("confidence") == "approx"
            and str(timing.get("source") or "").startswith(("net_snapshots", "spans+net"))):
        lines.insert(4, "- ожидание: оценка по снимкам сети, точность до 12 мин")
    queue = timing.get("queue_ms")
    if isinstance(queue, (int, float)) and queue > 0:
        lines.insert(3, f"- ожидание запуска: {T.format_duration(queue)}")
    profiles = summary.get("profiles") or {}
    if profiles:
        lines.append("- профили недоступны: " + ", ".join(f"{k} {_time(v)}" for k, v in profiles.items()))
    providers = summary.get("providers") or {}
    if providers:
        lines.append("- провайдеры недоступны: " + ", ".join(f"{k} {_time(v)}" for k, v in providers.items()))
    if summary.get("blocked_s"):
        lines.append(f"- заблокировано: {_time(summary['blocked_s'])}")
    lines += ["", "## Снимки", ""]
    for snap in data.get("snapshots") or []:
        mark = {"start": "старт", "long": "долго", "end": "конец"}.get(snap.get("scope"), snap.get("scope", ""))
        parts = [f"**{mark}**", _time(snap.get("elapsed_s")), f"сеть: {snap.get('state', '?')}"]
        req = snap.get("requirements") or {}
        if not (req.get("llm") or req.get("profiles") or req.get("internet")):
            parts.append("сеть не нужна")
        system = snap.get("system_vpn") or {}
        services = system.get("services") or []
        if services:
            parts.append("VPN: " + ", ".join(f"{s.get('name')} ({s.get('kind') or 'vpn'})" for s in services))
        blocked = snap.get("blocked") or []
        if blocked:
            parts.append("блокирует: " + ", ".join(str(x.get("code") or x.get("name") or "")
                                                   for x in blocked))
        lines.append("- " + " · ".join(str(x) for x in parts if x))
    return "\n".join(lines)


def list_reports(projects, since: float = 0.0, limit: int = 200) -> list:
    """Снимки сети по всем проектам для раздела «Замеры»: одна строка на прогон.

    Читает только общие части участников (`net-runs.d`), пути и адреса наружу не отдаёт.
    Свежие сверху."""
    out = []
    for project in projects or []:
        root = Path(project).expanduser().resolve().joinpath(*NO.SHARED_DIR)
        if not root.is_dir():
            continue
        try:
            parts = [p for p in root.iterdir() if p.is_dir()]
        except OSError:
            continue
        for part in parts:
            for path in part.glob("*.json"):
                try:
                    if since and path.stat().st_mtime < since:
                        continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                summary = data.get("summary") or NO.summarize(data)
                meta = _meta(str(project), str(data.get("id") or path.stem))
                timing = _timing_of(meta, summary.get("seconds"), summary)
                snaps = [s for s in data.get("snapshots") or [] if isinstance(s, dict)]
                vpn = sorted({str(s.get("name") or "")
                              for snap in snaps
                              for s in ((snap.get("system_vpn") or {}).get("services") or [])
                              if s.get("name")})
                out.append({
                    "project": os.path.basename(os.path.normpath(str(project))),
                    "run": str(data.get("id") or path.stem),
                    "cmd": str(data.get("cmd") or ""),
                    "at": str((snaps[-1] if snaps else {}).get("at") or ""),
                    "seconds": int(summary.get("seconds") or 0),
                    "timing": timing,
                    "snapshots": int(summary.get("snapshots") or 0),
                    "blocked_s": int(summary.get("blocked_s") or 0),
                    "reason": str(summary.get("reason") or ""),
                    "network_required": bool(summary.get("network_required")),
                    "profiles": summary.get("profiles") or {},
                    "providers": summary.get("providers") or {},
                    "vpn": vpn,
                })
    out.sort(key=lambda r: (r.get("at") or ""), reverse=True)
    return out[:max(0, int(limit or 0))] if limit else out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="net_report.py",
                                 description="Почему прогон шёл долго: сеть, VPN и провайдеры.")
    ap.add_argument("--project", default=".", help="корень проекта")
    ap.add_argument("--run", required=True, help="номер прогона из архива")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    a = ap.parse_args(argv)
    project = str(Path(a.project).expanduser().resolve())
    data = NO.load_run(project, str(a.run))
    if not data:
        print(f"net_report: нет снимков сети для прогона {a.run} в {project}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(_show(data, project))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
