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


def _show(data: dict) -> str:
    summary = data.get("summary") or NO.summarize(data)
    timing = T.legacy_timing(summary.get("seconds"), summary)
    lines = [f"# Сеть прогона {data.get('id', '')}", "",
             f"- команда: {data.get('cmd') or '—'}",
             f"- длительность: {T.format_work_wait(timing)}",
             f"- снимков: {summary.get('snapshots', 0)}"
             + (f" · долгих: {sum(1 for s in data.get('snapshots') or [] if s.get('scope') == 'long')}"
                if data.get("snapshots") else ""),
             f"- сеть нужна: {'да' if summary.get('network_required') else 'нет'}",
             f"- вывод: {summary.get('reason') or '—'}"]
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
                timing = T.legacy_timing(summary.get("seconds"), summary)
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
        print(_show(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
