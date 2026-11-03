#!/usr/bin/env python3
"""code_check.py — проверка критериев приёмки по индексу кода, без модели и судьи.

Пакет требований (`code:pack`) даёт критерии. Индекс (`code:index`) даёт факты: маршруты,
таблицы, топики, символы, строки, тесты. Скрипт сопоставляет критерий с фактами и выдаёт
доказательства `файл:строка`, а не вердикт «сделано». Чего нет в индексе, то и не найдено;
это повод проверить код, а не доказательство отсутствия.

  python3 .aurora/scripts/code_check.py --story US-1.1
  python3 .aurora/scripts/code_check.py --story US-1.1 --json
  python3 .aurora/scripts/code_check.py --story US-1.1 --out Artifacts/checks/US-1.1.md

Панель: `code:check`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_pack as CP  # noqa: E402
import code_repos as CREPO  # noqa: E402
from aurora_common import engine_dir  # noqa: E402
import codeidx.store as store_mod  # noqa: E402

METHOD_RX = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s,.;]+)", re.I)
PATH_RX = re.compile(r"(?<![\w])/(?:[A-Za-z0-9_{}./-]+)")
QUOTED_RX = re.compile(r"`([^`]{1,120})`|\"([^\"]{1,120})\"|«([^»]{1,120})»")
NAME_RX = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def _indexes(project: str) -> list:
    """[(имя репозитория, Store)] для построенных индексов."""
    out = []
    try:
        clean, _probs = CREPO.check(CREPO.read_repos(project))
    except Exception:  # noqa: BLE001 — нет реестра: проверка пустая, не падение
        return out
    for name in clean:
        db = os.path.join(project, engine_dir(project), "cache", "code", name, "index.sqlite")
        if not os.path.isfile(db):
            continue
        try:
            out.append((name, store_mod.Store(db)))
        except Exception:  # noqa: BLE001 — битый индекс одного репозитория
            continue
    return out


def _rows(store, sql: str, args=()) -> list:
    try:
        return list(store.conn.execute(sql, args))
    except Exception:  # noqa: BLE001 — в старом индексе может не быть таблицы
        return []


def _quoted(text: str) -> list:
    out = []
    for m in QUOTED_RX.finditer(text or ""):
        value = next(x for x in m.groups() if x)
        if value.strip():
            out.append(value.strip())
    return out


def _matches(text: str, *values: str) -> bool:
    low = (text or "").casefold()
    return any(v and v.casefold() in low for v in values)


def _evidence_for(criterion: dict, indexes: list) -> list:
    text = criterion.get("text") or ""
    found = []
    methods = {(m.group(1).upper(), m.group(2).rstrip(".,;")) for m in METHOD_RX.finditer(text)}
    paths = {p.rstrip(".,;") for p in PATH_RX.findall(text)}
    quoted = _quoted(text)
    names = {n for n in NAME_RX.findall(text) if n not in {"POST", "GET", "PUT", "DELETE", "PATCH"}}
    for repo, st in indexes:
        for row in _rows(st, "SELECT method, path, file, line, handler FROM routes ORDER BY path"):
            if (row["method"], row["path"]) in methods or row["path"] in paths or _matches(text, row["path"]):
                found.append({"repo": repo, "kind": "route", "file": row["file"], "line": row["line"],
                              "quote": f'{row["method"]} {row["path"]}',
                              "symbol": row["handler"]})
        for row in _rows(st, "SELECT table_name, file, line, source FROM db_tables ORDER BY table_name"):
            if _matches(text, row["table_name"]):
                found.append({"repo": repo, "kind": "table", "file": row["file"], "line": row["line"],
                              "quote": row["table_name"], "symbol": ""})
        for row in _rows(st, "SELECT name, file, line, role, symbol FROM topics ORDER BY name"):
            if _matches(text, row["name"]):
                found.append({"repo": repo, "kind": "topic", "file": row["file"], "line": row["line"],
                              "quote": row["name"], "symbol": row["symbol"]})
        for name in sorted(names):
            for row in _rows(st, "SELECT name, qname, file, line_start FROM symbols "
                                 "WHERE name = ? OR qname = ? LIMIT 4", (name, name)):
                found.append({"repo": repo, "kind": "symbol", "file": row["file"],
                              "line": row["line_start"], "quote": row["qname"] or row["name"],
                              "symbol": row["qname"] or row["name"]})
        for value in quoted:
            rows = _rows(st, "SELECT text, file, line, symbol FROM strings "
                             "WHERE text = ? OR text LIKE ? LIMIT 4", (value, f"%{value}%"))
            for row in rows:
                found.append({"repo": repo, "kind": "string", "file": row["file"], "line": row["line"],
                              "quote": row["text"], "symbol": row["symbol"]})
        for row in _rows(st, "SELECT name, file, line FROM tests ORDER BY name"):
            if _matches(text, row["name"]):
                found.append({"repo": repo, "kind": "test", "file": row["file"], "line": row["line"],
                              "quote": row["name"], "symbol": ""})
    # Один и тот же факт из двух репозиториев — две строки; оставляем точные координаты.
    uniq = []
    seen = set()
    for e in found:
        key = (e["repo"], e["kind"], e["file"], e["line"], e["quote"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq[:12]


def _missing(criterion: dict, evidence: list) -> list:
    """Токены критерия, которых нет в найденных доказательствах (числа, коды, версии, даты)."""
    hay = " ".join(f'{e.get("quote", "")} {e.get("file", "")}' for e in evidence).casefold()
    out = []
    for tok in criterion.get("tokens") or []:
        value = str(tok.get("value") or "")
        if value and value.casefold() not in hay:
            out.append(tok.get("text") or value)
    return out


def check_story(project: str, story: str) -> dict:
    pack = CP.build_pack(project, story)
    if not pack.get("found"):
        return {"story": story.upper(), "found": False, "criteria": [], "summary": {}}
    indexes = _indexes(project)
    rows = []
    for criterion in pack.get("criteria") or []:
        evidence = _evidence_for(criterion, indexes)
        missing = _missing(criterion, evidence)
        verdict = "found" if evidence and not missing else "partial" if evidence else "not_found"
        rows.append({**criterion, "verdict": verdict, "evidence": evidence, "missing": missing})
    summary = {"total": len(rows),
               "found": sum(r["verdict"] == "found" for r in rows),
               "partial": sum(r["verdict"] == "partial" for r in rows),
               "not_found": sum(r["verdict"] == "not_found" for r in rows),
               "repos": [name for name, _ in indexes]}
    return {"story": story.upper(), "found": True, "criteria": rows, "summary": summary}


def render_md(report: dict) -> str:
    lines = [f"# {report['story']}. Проверка критериев по коду", ""]
    if not report.get("found"):
        return "\n".join(lines + ["История не найдена.", ""])
    s = report["summary"]
    lines += [f"Критериев {s['total']}: найдено {s['found']}, частично {s['partial']}, "
              f"не найдено {s['not_found']}. Индекс: {', '.join(s['repos']) or 'нет'}.", ""]
    for crit in report["criteria"]:
        icon = {"found": "✅", "partial": "◐", "not_found": "·"}[crit["verdict"]]
        lines.append(f"## {icon} {crit['text']}")
        lines.append("")
        if crit.get("missing"):
            lines.append(f"Не найдено: {', '.join(crit['missing'])}")
            lines.append("")
        if crit.get("evidence"):
            lines.append("| Вид | Место | Факт |")
            lines.append("|---|---|---|")
            for e in crit["evidence"]:
                lines.append(f"| {e['kind']} | `{e['file']}:{e['line']}` | {e['quote']} |")
        else:
            lines.append("Доказательств в индексе нет.")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Проверка критериев истории по индексу кода")
    p.add_argument("--project", default=".", help="корень проекта")
    p.add_argument("--story", required=True, help="номер истории")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--out", default="", help="записать Markdown")
    a = p.parse_args(argv)
    root = Path(a.project).resolve()
    report = check_story(str(root), a.story)
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        text = render_md(report)
        if a.out:
            out = root / a.out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            print(f"Записано: {a.out}")
        else:
            print(text)
    return 0 if report.get("found") else 1


if __name__ == "__main__":
    sys.exit(main())
