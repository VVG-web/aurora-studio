#!/usr/bin/env python3
"""graphify_adapter.py — graphify под капотом Авроры, если он установлен.

Как и адаптер Pydantic AI: graphify живёт в своём venv (`~/.aurora/graphify`, ставится
из «Установки» панели), а движок зовёт его подпроцессом и получает JSON. Не стоит —
каждая функция возвращает пустой ответ, и движок делает то же своей механикой.

Что берём у graphify (разбор 27.09.2026, 1.138.0):
  • `cluster` — темы базы Лейденом вместо нашего Лувена;
  • `serve_argv` — готовый MCP-сервер графа базы для Claude Code, Cursor и агентов;
  • `code_graph` — разбор кода и SQL проекта без модели (tree-sitter).
Разбор документов моделью у graphify не берём: он стоит тех же токенов, что наш.
Страницу графа тоже не берём: у graphify она тянет vis-network из сети, наша
(`kb_graph.graph_page`) открывается без неё.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def python() -> str:
    """Питон venv graphify, если graphify в нём стоит. Пусто — не стоит."""
    try:
        import aurora_extras as EX
    except ImportError:
        return ""
    vpy = EX.venv_python("graphify")
    return str(vpy) if vpy.is_file() and EX.installed_version("graphify") else ""


def _call(script: str, payload: dict, timeout: int = 600) -> dict | None:
    vpy = python()
    if not vpy:
        return None
    import shutil
    import tempfile
    # Свой кэш graphify пишет в `graphify-out/` текущей папки — в проекте это был бы мусор
    # в корне. Даём ему временную папку и убираем её после вызова.
    scratch = tempfile.mkdtemp(prefix="aurora-graphify-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("Malloc")}
    env["GRAPHIFY_OUT"] = os.path.join(scratch, "graphify-out")
    try:
        p = subprocess.run([vpy, "-c", script], input=json.dumps(payload, ensure_ascii=False),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
                           cwd=scratch)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


_CLUSTER = r"""
import json, sys
import networkx as nx
from graphify.cluster import cluster
d = json.load(sys.stdin)
G = nx.Graph()
G.add_nodes_from(d["nodes"])
G.add_edges_from(d["edges"])
got = cluster(G)
print(json.dumps({str(k): v for k, v in got.items()}, ensure_ascii=False))
"""


def cluster(pairs: dict) -> dict | None:
    """{узел: номер темы} — Лейден graphify. None — graphify не стоит или не справился."""
    nodes = sorted(pairs)
    edges = sorted({tuple(sorted((a, b))) for a, nbrs in pairs.items() for b in nbrs if a != b})
    got = _call(_CLUSTER, {"nodes": nodes, "edges": [list(e) for e in edges]})
    if not got:
        return None
    # Номера тем — по размеру, как у graphify: 1 — самая крупная.
    groups = sorted(got.values(), key=lambda m: (-len(m), sorted(m)[:1]))
    return {n: i for i, members in enumerate(groups, 1) for n in members}


def serve_argv(graph_path: str) -> list:
    """Команда MCP-сервера графа базы. Пусто — graphify не стоит."""
    vpy = python()
    return [vpy, "-m", "graphify.serve", os.path.abspath(graph_path)] if vpy else []


_CYPHER = r"""
import json, sys
from networkx.readwrite import json_graph
from graphify.export import to_cypher
d = json.load(sys.stdin)
data = json.load(open(d["graph"], encoding="utf-8"))
G = json_graph.node_link_graph(data, edges="links")
to_cypher(G, d["out"])
print(json.dumps({"ok": True}))
"""


def to_cypher(graph_path: str, out_path: str) -> bool:
    """Скрипт Cypher для Neo4j по `graph.json` (MERGE — повтор не удваивает). False — не вышло."""
    got = _call(_CYPHER, {"graph": os.path.abspath(graph_path), "out": os.path.abspath(out_path)})
    return bool(got and got.get("ok"))


_CODE = r"""
import json, sys
from pathlib import Path
from graphify.extract import extract, collect_files
d = json.load(sys.stdin)
root = Path(d["root"]).resolve()
files = []
for folder in d["dirs"]:
    files += collect_files(Path(folder).resolve())
res = extract(files, root=root, parallel=False) if files else {"nodes": [], "edges": []}
print(json.dumps({"files": len(files), "nodes": res.get("nodes", []),
                  "edges": res.get("edges", [])}, ensure_ascii=False, default=str))
"""


def code_graph(root: str, dirs: list) -> dict | None:
    """Узлы и связи кода и SQL из папок проекта — синтаксическим разбором, без модели."""
    return _call(_CODE, {"root": os.path.abspath(root), "dirs": [os.path.abspath(d) for d in dirs]},
                 timeout=1800)
