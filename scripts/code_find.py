#!/usr/bin/env python3
"""code_find.py — поиск по индексу кода проекта: символы, маршруты, таблицы, топики, коммиты.

Ищет по уже построенному индексу (`code:index`), модель и сеть не нужны. Пустая выдача —
не ошибка: код может просто не содержать такого имени.

  python3 .aurora/scripts/code_find.py --query "orders"
  python3 .aurora/scripts/code_find.py "orders.accepted" --kind topic
  python3 .aurora/scripts/code_find.py "BENCH-1" --json
  python3 .aurora/scripts/code_find.py "OrderRequest" --repo order-intake --limit 20

Панель: `code:find`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_repos as CREPO  # noqa: E402
import codeidx.query as query  # noqa: E402
import codeidx.store as store_mod  # noqa: E402
from aurora_common import engine_dir  # noqa: E402

EXIT_OK, EXIT_FAIL = 0, 2


def index_path(project, name: str) -> Path:
    return Path(project) / engine_dir(project) / "cache" / "code" / name / "index.sqlite"


def selected(project, only: str) -> tuple:
    clean, probs = CREPO.check(CREPO.read_repos(project))
    names = list(clean)
    if only:
        hit = CREPO.find(clean, only)
        if not hit:
            return clean, [], probs, only
        names = [hit]
    return clean, names, probs, ""


def _say(*a) -> None:
    print(*a, flush=True)


def run(project, names: list, text: str, kind: str, limit: int, as_json: bool) -> int:
    rows = []
    missing = []
    for name in names:
        path = index_path(project, name)
        if not path.is_file():
            missing.append(name)
            continue
        with store_mod.Store(path) as st:
            for rec in query.find(st, text, kind=kind, limit=limit):
                rows.append({"repo": name, **rec})
    if as_json:
        _say(json.dumps({"query": text, "results": rows, "missing": missing}, ensure_ascii=False))
        return EXIT_OK if not missing else EXIT_FAIL
    if missing:
        for name in missing:
            _say(f"✗ {name}: индекс не построен — выполните code:index --apply")
    if not rows:
        _say(f"По «{text}» ничего не найдено." if text.strip() else "Пустой запрос.")
        return EXIT_OK if not missing else EXIT_FAIL
    _say(f"По «{text}» найдено {len(rows)}:")
    for rec in rows:
        where = f"{rec['file']}:{rec['line']}" if rec.get("file") else ""
        detail = f" — {rec['detail']}" if rec.get("detail") else ""
        repo = f"[{rec['repo']}] " if len(names) > 1 else ""
        _say(f"· {repo}[{rec['type']}] {rec['name']}{detail}" + (f"  {where}" if where else ""))
    return EXIT_OK if not missing else EXIT_FAIL


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Поиск по индексу кода проекта")
    p.add_argument("query", nargs="?", default="", help="строка поиска")
    p.add_argument("--query", dest="query_opt", default="", help="строка поиска (альтернатива позиционной)")
    p.add_argument("--project", default=".", help="корень проекта (по умолчанию текущая папка)")
    p.add_argument("--repo", default="", help="только один репозиторий по имени")
    p.add_argument("--kind", default="", choices=("", *query.KINDS), help="вид записи")
    p.add_argument("--limit", type=int, default=50, help="сколько результатов (по умолчанию 50)")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    args = p.parse_args(argv)
    project = Path(args.project).resolve()
    text = args.query_opt or args.query
    clean, names, probs, unknown = selected(project, args.repo)
    if unknown:
        _say(f"✗ репозитория «{unknown}» нет в реестре кода — подключите его в разделе «Код»")
        return EXIT_FAIL
    bad = {x["name"] for x in probs}
    for x in probs:
        line = (f"✗ {x['name']}: запись реестра не годится — поле {x['field']} ({x['code']}); "
                "поправьте в разделе «Код»")
        print(line, file=sys.stderr if args.json else sys.stdout, flush=True)
    names = [n for n in names if n not in bad]
    if not names:
        _say("Репозиториев кода в реестре нет — искать негде (подключаются в разделе «Код»).")
        return EXIT_OK
    return run(project, names, text, args.kind, max(1, args.limit), args.json)


if __name__ == "__main__":
    sys.exit(main())
