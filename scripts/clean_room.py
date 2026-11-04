#!/usr/bin/env python3
"""clean_room.py — артефакты «как сделано» по коду, без модели.

Читает индекс `code:index`, собирает факты по каждому репозиторию и пишет документы в
`CleanRoom/<вид>/<репозиторий>.md`. Каждое утверждение имеет ссылку `[cN]` на `файл:строка`;
чего нет в индексе, попадает в «Что не удалось установить по коду».

  python3 .aurora/scripts/clean_room.py --apply
  python3 .aurora/scripts/clean_room.py --check
  python3 .aurora/scripts/clean_room.py --json

Паспорт `CleanRoom/meta/passport.json` хранит ревизию и время; `--check` называет устаревшие
и отсутствующие документы. Модель и сеть не нужны.

Панель: `code:cleanroom`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_artifacts as CA  # noqa: E402
import code_repos as CREPO  # noqa: E402
from aurora_common import TODAY, engine_dir  # noqa: E402
import codeidx.store as store_mod  # noqa: E402

OUT = "CleanRoom"
PASSPORT = "CleanRoom/meta/passport.json"


def _indexes(project: str) -> list:
    out = []
    try:
        clean, _probs = CREPO.check(CREPO.read_repos(project))
    except Exception:  # noqa: BLE001
        return out
    for name in clean:
        db = os.path.join(project, engine_dir(project), "cache", "code", name, "index.sqlite")
        if not os.path.isfile(db):
            continue
        try:
            st = store_mod.Store(db)
            out.append((name, st, st.status()))
        except Exception:  # noqa: BLE001
            continue
    return out


def _facts(st, kind: str) -> list:
    """(подпись, файл, строка) для вида; только то, что индекс действительно знает."""
    rows = []
    if kind in ("cr-api", "cr-module"):
        for r in st.conn.execute("SELECT method, path, file, line FROM routes ORDER BY path LIMIT 40"):
            rows.append((f'{r["method"]} {r["path"]}', r["file"], r["line"]))
    if kind in ("cr-data", "cr-crud"):
        for r in st.conn.execute("SELECT table_name, file, line FROM db_tables "
                                 "ORDER BY table_name LIMIT 40"):
            rows.append((r["table_name"], r["file"], r["line"]))
    if kind == "cr-events":
        for r in st.conn.execute("SELECT name, file, line FROM topics ORDER BY name LIMIT 40"):
            rows.append((r["name"], r["file"], r["line"]))
    if kind in ("cr-algorithm", "cr-rules", "cr-module"):
        for r in st.conn.execute("SELECT name, qname, file, line_start FROM symbols "
                                 "ORDER BY file, line_start LIMIT 40"):
            rows.append((r["qname"] or r["name"], r["file"], r["line_start"]))
    if kind == "cr-tests":
        for r in st.conn.execute("SELECT symbol, target, file, line FROM tests ORDER BY symbol LIMIT 40"):
            rows.append((r["target"] or r["symbol"], r["file"], r["line"]))
    if kind == "cr-config":
        for r in st.conn.execute("SELECT key, file, line FROM configs ORDER BY key LIMIT 40"):
            rows.append((r["key"], r["file"], r["line"]))
    return rows


def _render(name: str, kind: str, title: str, rev: str, facts: list) -> str:
    lines = ["---", f'title: "{kind} — {name}"', f"kind: {kind}", f"source_repo: {name}",
             f"source_revision: {rev}", f"generated: {TODAY}", "generator: clean_room", "---", "",
             f"# {title}: {name}", "",
             f"Ревизия `{rev}`. Документ собран по индексу кода; утверждения без ссылки `[cN]` "
             "не являются фактами.", "", "## Что видно по коду", ""]
    if not facts:
        lines.append("Индекс не дал фактов этого вида.")
    for i, (label, file, line) in enumerate(facts, 1):
        lines.append(f"- {label} [c{i}]")
        lines.append(f"  - `{file}:{line}`")
    lines += ["", "## Что не удалось установить по коду", "",
              "Смысл, причины решений и поведение вне индекса здесь не восстановлены: "
              "для этого нужен человек или отдельная проверка.", "",
              "## Источники", ""]
    for i, (_label, file, line) in enumerate(facts, 1):
        lines.append(f"- [c{i}] `{file}:{line}`")
    return "\n".join(lines) + "\n"


def build(project: str) -> dict:
    kinds, _problems = CA.check(project)
    enabled = [rec for rec in kinds.values() if rec.get("enabled")]
    indexes = _indexes(project)
    docs = []
    for name, st, status in indexes:
        rev = status.get("repo_rev") or status.get("index_rev") or ""
        for rec in enabled:
            kind = rec.get("kind") or ""
            facts = _facts(st, kind)
            rel = f"{OUT}/{kind}/{name}.md"
            docs.append({"repo": name, "kind": kind, "path": rel, "rev": rev,
                         "title": rec.get("title") or kind, "facts": len(facts),
                         "text": _render(name, kind, rec.get("title") or kind, rev, facts)})
    return {"built": TODAY, "docs": docs,
            "repos": {name: status for name, _st, status in indexes}}


def write(project: str, data: dict) -> None:
    root = Path(project)
    for doc in data["docs"]:
        path = root / doc["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc["text"], encoding="utf-8")
    passport = {"built": data["built"],
                "repos": {name: {"rev": st.get("repo_rev") or st.get("index_rev") or "",
                                 "indexed_at": st.get("indexed_at") or "",
                                 "status": "built"}
                          for name, st in data["repos"].items()},
                "docs": [{"repo": d["repo"], "kind": d["kind"], "path": d["path"],
                          "rev": d["rev"], "facts": d["facts"]} for d in data["docs"]]}
    path = root / PASSPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(passport, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def check(project: str) -> dict:
    root = Path(project)
    path = root / PASSPORT
    if not path.is_file():
        return {"ok": False, "missing": [PASSPORT], "stale": []}
    try:
        passport = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"ok": False, "missing": [PASSPORT], "stale": []}
    missing, stale = [], []
    for doc in passport.get("docs") or []:
        if not (root / doc["path"]).is_file():
            missing.append(doc["path"])
    for name, rec in (passport.get("repos") or {}).items():
        db = os.path.join(project, engine_dir(project), "cache", "code", name, "index.sqlite")
        if not os.path.isfile(db):
            stale.append({"repo": name, "why": "индекса нет"})
            continue
        try:
            with store_mod.Store(db) as st:
                rev = st.status().get("repo_rev") or st.status().get("index_rev") or ""
        except Exception:  # noqa: BLE001
            rev = ""
        if rev and rec.get("rev") and rev != rec["rev"]:
            stale.append({"repo": name, "why": f"{rec['rev'][:12]} → {rev[:12]}"})
    return {"ok": not missing and not stale, "missing": missing, "stale": stale}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Clean Room: артефакты по коду")
    p.add_argument("--project", default=".", help="корень проекта")
    p.add_argument("--apply", action="store_true", help="записать документы и паспорт")
    p.add_argument("--check", action="store_true", help="проверить паспорт и устаревание")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    a = p.parse_args(argv)
    root = str(Path(a.project).resolve())
    if a.check:
        res = check(root)
        print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else
              ("✅ Clean Room актуален" if res["ok"] else
               f"⚠️ отсутствует: {len(res['missing'])} · устарело: {len(res['stale'])}"))
        return 0 if res["ok"] else 1
    data = build(root)
    if a.json:
        print(json.dumps({k: v for k, v in data.items() if k != "docs"} |
                         {"docs": [{k: v for k, v in d.items() if k != "text"} for d in data["docs"]]},
                         ensure_ascii=False, indent=1))
    else:
        print(f"Репозиториев {len(data['repos'])} · документов {len(data['docs'])}")
        for d in data["docs"]:
            print(f"  - {d['path']}: фактов {d['facts']}")
    if a.apply:
        write(root, data)
        print(f"Записано: {OUT}/ и {PASSPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
