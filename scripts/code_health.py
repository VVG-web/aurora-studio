#!/usr/bin/env python3
"""code_health.py — здоровье индексов кода: свежесть, ревизия, объём, устаревание.

Смотрит каждый подключённый репозиторий `Code/`, его индекс и текущую ревизию клона.
Ничего не пишет. `--check` возвращает 1, если индекс отсутствует или отстал от клона:
это сигнал для cron-перепроверки, а не ошибка панели.

  python3 .aurora/scripts/code_health.py
  python3 .aurora/scripts/code_health.py --json
  python3 .aurora/scripts/code_health.py --check

Панель: `code:health`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_repos as CREPO  # noqa: E402
import codeidx.scan as scan  # noqa: E402
from aurora_common import engine_dir  # noqa: E402
import codeidx.store as store_mod  # noqa: E402


def _age(indexed_at: str) -> str:
    try:
        stamp = time.mktime(time.strptime(indexed_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError, OverflowError):
        return ""
    seconds = max(0, int(time.time() - stamp))
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} дн"


def state(project: str) -> dict:
    rows = []
    try:
        clean, probs = CREPO.check(CREPO.read_repos(project))
    except Exception as e:  # noqa: BLE001
        return {"repos": [], "failed": f"{type(e).__name__}: {e}"}
    for name in clean:
        clone = CREPO.clone_path(project, name)
        db = os.path.join(project, engine_dir(project), "cache", "code", name, "index.sqlite")
        row = {"name": name, "clone": str(clone), "index": db, "built": os.path.isfile(db),
               "rev": "", "index_rev": "", "indexed_at": "", "age": "", "files": 0,
               "routes": 0, "tables": 0, "topics": 0, "symbols": 0, "stale": False,
               "why": ""}
        if clone.is_dir():
            try:
                row["rev"] = scan.GitRepo(clone).head()
            except Exception:  # noqa: BLE001
                row["why"] = "клон не читается"
        else:
            row["why"] = "клона нет"
        if row["built"]:
            try:
                with store_mod.Store(db) as st:
                    status = st.status()
                    row.update({k: status.get(k, row[k]) for k in
                                ("index_rev", "indexed_at", "files", "routes", "tables",
                                 "topics", "symbols")})
                    row["age"] = _age(row["indexed_at"])
            except Exception:  # noqa: BLE001
                row["why"] = "индекс не открывается"
        row["stale"] = bool(row["rev"] and row["index_rev"] and row["rev"] != row["index_rev"])
        if row["stale"]:
            row["why"] = f'{row["index_rev"][:12]} → {row["rev"][:12]}'
        rows.append(row)
    return {"repos": rows, "problems": probs, "failed": ""}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Здоровье индексов кода")
    p.add_argument("--project", default=".", help="корень проекта")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--check", action="store_true", help="код 1, если индекс отсутствует или устарел")
    a = p.parse_args(argv)
    data = state(str(Path(a.project).resolve()))
    bad = [r for r in data["repos"] if not r["built"] or r["stale"] or r.get("why")]
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        for r in data["repos"]:
            mark = "✅" if r["built"] and not r["stale"] and not r.get("why") else "⚠️"
            print(f'{mark} {r["name"]}: ревизия {r["rev"][:12] or "—"} · индекс '
                  f'{r["index_rev"][:12] or "—"} · {r["age"] or "нет"} · файлов {r["files"]} · '
                  f'маршрутов {r["routes"]} · таблиц {r["tables"]} · топиков {r["topics"]}'
                  + (f' · {r["why"]}' if r.get("why") else ""))
    return 1 if a.check and bad else 0


if __name__ == "__main__":
    sys.exit(main())
