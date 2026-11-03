#!/usr/bin/env python3
"""kb_code_graph.py — код и SQL проекта в графе базы, без модели (фреймворк «Аврора»).

Аналитика говорит о системе, код и SQL — сама система. Связь между ними уже записана:
карточка называет таблицу (`order_log`), запрос соединяет таблицы (`JOIN`). Скрипт
собирает это механикой и кладёт в `meta/graphify/code.json`; `kb:graph-export` добавляет
в граф базы таблицы и код, а MCP-сервер графа даёт агенту пройти «требование → таблица →
связанные таблицы».

Откуда берём:
  • SQL-запросы — блоки ```sql на страницах зеркала Confluence и файлы `*.sql` проекта:
    таблицы из `FROM`/`JOIN`, пары таблиц одного запроса;
  • код — папки из `graphify: code_dirs: [...]` в aurora.config.yaml: их разбирает
    graphify (tree-sitter, без модели), если он установлен (см. «Установка» панели).

  python3 .aurora/scripts/kb_code_graph.py            # что нашлось
  python3 .aurora/scripts/kb_code_graph.py --apply    # записать meta/graphify/code.json

Панель: `kb:code-graph`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import TODAY, config_list, service_dirs, walk_md  # noqa: E402
from aurora_common import CODE_DIR, safe_walk  # noqa: E402 — обход без ссылок за пределы проекта

KB = "AuroraKnowledgeDB"
OUT = os.path.join(KB, "meta", "graphify", "code.json")
SQL_BLOCK = re.compile(r"```sql\s*\n(.*?)```", re.S | re.I)
TABLE = re.compile(r"\b(?:from|join|update|into)\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)", re.I)
# Слова SQL, которые встают после FROM/JOIN, но таблицами не являются.
NOT_TABLE = {"select", "dual", "lateral", "unnest", "generate_series", "values", "table",
             "only", "json_array_elements", "jsonb_array_elements", "set"}
SKIP_DIRS = {".git", ".aurora", "node_modules", "_archive", "venv", ".venv"}


def sql_sources(root: str) -> list:
    """[(откуда, текст SQL)] — блоки со страниц зеркала и файлы `*.sql` проекта."""
    out = []
    wikis = [os.path.join(root, d) for d in service_dirs("projectwiki", root)]
    for p in (q for w in wikis if os.path.isdir(w) for q in walk_md(w)):
        text = open(p, encoding="utf-8", errors="ignore").read()
        for m in SQL_BLOCK.finditer(text):
            # Зеркало экранирует разметку: `\_` в запросе — это `_`.
            out.append((os.path.relpath(p, root).replace("\\", "/"),
                        m.group(1).replace("\\_", "_").replace("\\*", "*")))
    for base, dirs, files in safe_walk(root):
        # Клоны репозиториев кода (`Code/` у корня) — не SQL проекта: их код в граф придёт через индекс кода, а не
        # обходом всех файлов (на клоне в десятки тысяч файлов этот обход стоил бы минут).
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                   and not (base == root and d.casefold() == CODE_DIR.casefold())]
        for f in files:
            if f.lower().endswith(".sql"):
                p = os.path.join(base, f)
                out.append((os.path.relpath(p, root).replace("\\", "/"),
                            open(p, encoding="utf-8", errors="ignore").read()))
    return out


def parse_sql(sources: list) -> tuple:
    """({таблица: {uses, sources}}, {(таблица, таблица): сколько запросов соединяют})."""
    tables: dict = {}
    pairs: dict = {}
    for src, code in sources:
        # Комментарии запроса — не запрос: «-- номер из orders» таблицы не называет.
        code = re.sub(r"--[^\n]*", " ", code)
        code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
        for q in re.split(r";\s*", code):
            # `public.x` и `x` — одна таблица: схема по умолчанию именем не считается.
            names = sorted({re.sub(r"^public\.", "", t.lower()) for t in TABLE.findall(q)
                            if len(t) > 3 and t.lower() not in NOT_TABLE})
            for t in names:
                rec = tables.setdefault(t, {"uses": 0, "sources": []})
                rec["uses"] += 1
                if src not in rec["sources"]:
                    rec["sources"].append(src)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    key = (names[i], names[j])
                    pairs[key] = pairs.get(key, 0) + 1
    return tables, pairs


def card_mentions(root: str, names: list) -> dict:
    """{таблица: [карточки]} — карточки, которые называют таблицу дословно.

    Только имена с подчёркиванием или схемой (`order_log`, `ref.goods`): слово
    без них («orders», «vendor») совпадает с обычным текстом и связь была бы выдумана.
    """
    wanted = {}
    for n in names:
        short = n.split(".")[-1]
        if "_" in short and len(short) >= 5:
            wanted[n] = re.compile(rf"(?<![\w.]){re.escape(short)}(?!\w)", re.I)
    out: dict = {}
    for p in walk_md(os.path.join(root, KB), skip_service=True, skip_archive=True):
        text = open(p, encoding="utf-8", errors="ignore").read().replace("\\_", "_")
        stem = os.path.basename(p)[:-3]
        # Оглавление перечисляет чужие карточки — таблицу называет не оно.
        if "/MOC/" in p.replace("\\", "/") or re.search(r"^status:\s*index", text, re.M):
            continue
        for n, rx in wanted.items():
            if rx.search(text):
                out.setdefault(n, []).append(stem)
    return out


def code_layer(root: str) -> dict:
    """Узлы и связи кода из `graphify: code_dirs` — разбором graphify, если он стоит."""
    dirs = [d for d in config_list("code_dirs") if os.path.isdir(os.path.join(root, d))]
    if not dirs:
        return {"dirs": [], "nodes": [], "edges": [], "note": ""}
    try:
        from agents import graphify_adapter as GA
    except ImportError:
        GA = None
    got = GA.code_graph(root, [os.path.join(root, d) for d in dirs]) if GA else None
    if got is None:
        return {"dirs": dirs, "nodes": [], "edges": [],
                "note": "graphify не установлен — код не разобран («Установка» → «Надстройки движка»)"}
    keep = ("id", "label", "file_type", "source_file", "source_location")
    return {"dirs": dirs, "note": "",
            "nodes": [{k: n.get(k) for k in keep} for n in got.get("nodes", [])],
            "edges": [{k: e.get(k) for k in ("source", "target", "relation", "confidence")}
                      for e in got.get("edges", [])]}


def index_layer(root: str) -> dict:
    """Таблицы, маршруты и топики из индекса кода (`code:index`), если он построен.

    Индекс — единственный источник, который знает код без обхода `Code/` в этом скрипте.
    Нет индекса или репозиториев — пустой слой, граф собирается как раньше.
    """
    out = {"repos": [], "tables": {}, "routes": [], "topics": []}
    try:
        import code_repos as CR
        from aurora_common import engine_dir
        import codeidx.store as store_mod
    except ImportError:
        return out
    try:
        clean, _probs = CR.check(CR.read_repos(root))
    except Exception:  # noqa: BLE001 — сломанный реестр не роняет граф
        return out
    for name in clean:
        db = os.path.join(root, engine_dir(root), "cache", "code", name, "index.sqlite")
        if not os.path.isfile(db):
            continue
        try:
            with store_mod.Store(db) as st:
                for row in st.conn.execute(
                        "SELECT table_name, file, source, line FROM db_tables "
                        "ORDER BY table_name, file"):
                    rec = out["tables"].setdefault(row["table_name"], {"uses": 0, "sources": []})
                    rec["uses"] += 1
                    if row["file"] not in rec["sources"]:
                        rec["sources"].append(row["file"])
                for row in st.conn.execute(
                        "SELECT method, path, handler, file, line FROM routes "
                        "ORDER BY path, method"):
                    rec = dict(row)
                    rec["repo"] = name
                    out["routes"].append(rec)
                for row in st.conn.execute(
                        "SELECT name, file, role, symbol, line FROM topics "
                        "ORDER BY name, file"):
                    rec = dict(row)
                    rec["repo"] = name
                    out["topics"].append(rec)
            out["repos"].append(name)
        except Exception:  # noqa: BLE001 — битый индекс одного репозитория не роняет остальные
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Код и SQL проекта в графе базы")
    ap.add_argument("--apply", action="store_true", help=f"записать {OUT.replace(os.sep, '/')}")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()
    root = a.root
    if not os.path.isdir(os.path.join(root, KB)):
        print("kb_code_graph: нет AuroraKnowledgeDB/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    sources = sql_sources(root)
    tables, pairs = parse_sql(sources)
    index = index_layer(root)
    for name, rec in index["tables"].items():
        cur = tables.setdefault(name, {"uses": 0, "sources": []})
        cur["uses"] += rec["uses"]
        for src in rec["sources"]:
            if src not in cur["sources"]:
                cur["sources"].append(src)
    mentions = card_mentions(root, sorted(tables))
    code = code_layer(root)
    print(f"# Код и SQL в графе базы — {TODAY}\n")
    print(f"SQL: источников {len(sources)} · таблиц {len(tables)} · пар таблиц в одном "
          f"запросе {len(pairs)} · таблиц, названных в карточках: {len(mentions)} "
          f"(упоминаний {sum(len(v) for v in mentions.values())})")
    if index["repos"]:
        print(f"Индекс кода ({', '.join(index['repos'])}): маршрутов {len(index['routes'])} · "
              f"топиков {len(index['topics'])} · таблиц {len(index['tables'])}")
    for t, cards in sorted(mentions.items(), key=lambda x: -len(x[1]))[:10]:
        print(f"  - {t}: " + ", ".join(f"[[{c}]]" for c in cards[:4])
              + (f" … ещё {len(cards) - 4}" if len(cards) > 4 else ""))
    if code["dirs"]:
        print(f"Код ({', '.join(code['dirs'])}): узлов {len(code['nodes'])} · связей "
              f"{len(code['edges'])}" + (f" — {code['note']}" if code["note"] else ""))
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    os.makedirs(os.path.dirname(os.path.join(root, OUT)), exist_ok=True)
    data = {"built": TODAY,
            "tables": {t: dict(v, cards=mentions.get(t, [])) for t, v in sorted(tables.items())},
            "joins": [{"a": a_, "b": b_, "queries": n} for (a_, b_), n in sorted(pairs.items())],
            "index": {"repos": index["repos"]},
            "routes": index["routes"],
            "topics": index["topics"],
            "code": code}
    with open(os.path.join(root, OUT), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\n✅ {OUT} — граф базы возьмёт его при следующем `kb:graph-export`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
