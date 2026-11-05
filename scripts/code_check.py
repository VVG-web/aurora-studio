#!/usr/bin/env python3
"""code_check.py — проверка критериев приёмки по индексу кода, детерминированно и с арбитром.

Пакет требований (`code:pack`) даёт критерии. Индекс (`code:index`) даёт факты: маршруты,
таблицы, топики, символы, строки, тесты. Скрипт сопоставляет критерий с фактами и выдаёт
доказательства `файл:строка`, а не вердикт «сделано». Чего нет в индексе, то и не найдено;
это повод проверить код, а не доказательство отсутствия.

Модель — только арбитр спорных критериев (`partial`/`not_found`) и только по запросу
`--agent`: она не видит код, опирается на собранные доказательства, обязана дать цитаты
`файл:строка` из списка и не может отменить детерминированный `found`.

  python3 .aurora/scripts/code_check.py --story US-1.1
  python3 .aurora/scripts/code_check.py --story US-1.1 --json
  python3 .aurora/scripts/code_check.py --story US-1.1 --agent --max-calls 3
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
import codeidx.scan as scan  # noqa: E402
import codeidx.store as store_mod  # noqa: E402

METHOD_RX = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[^\s,.;]+)", re.I)
PATH_RX = re.compile(r"(?<![\w])/(?:[A-Za-z0-9_{}./-]+)")
QUOTED_RX = re.compile(r"`([^`]{1,120})`|\"([^\"]{1,120})\"|«([^»]{1,120})»")
NAME_RX = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
TABLE_ID_RX = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")
DOTTED_RX = re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z0-9]+)+\b")
VERSION_RX = re.compile(r"^v?\d+(?:\.\d+)+$", re.I)


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


def _required(criterion: dict) -> list:
    """Имена, которые критерий называет: цитаты, таблицы, топики и маршруты."""
    text = criterion.get("text") or ""
    values = list(_quoted(text))
    values += [m.group(0) for m in TABLE_ID_RX.finditer(text)]
    values += [m.group(0) for m in DOTTED_RX.finditer(text) if not VERSION_RX.match(m.group(0))]
    values += [m.group(2).rstrip(".,;") for m in METHOD_RX.finditer(text)]
    return sorted({value for value in values if value.strip()})


def _missing_tokens(criterion: dict, hay: str, seen: set) -> list:
    out = []
    for tok in criterion.get("tokens") or []:
        value = str(tok.get("value") or "")
        text = tok.get("text") or value
        if value and value.casefold() not in hay and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _missing(criterion: dict, evidence: list) -> list:
    """Токены и имена критерия, которых нет в найденных доказательствах."""
    hay = " ".join(f'{e.get("quote", "")} {e.get("file", "")}' for e in evidence).casefold()
    seen = set()
    out = _missing_tokens(criterion, hay, seen)
    for value in _required(criterion):
        if value.casefold() not in hay and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _stale(project: str, indexes: list) -> list:
    """Индексы, отставшие от HEAD клона: проверка идёт по старым фактам."""
    out = []
    for name, st in indexes:
        try:
            status = st.status()
            rev = status.get("repo_rev") or status.get("index_rev") or ""
            clone = CREPO.clone_path(project, name)
            head = scan.GitRepo(clone).head() if clone.is_dir() else ""
            if rev and head and rev != head:
                out.append({"repo": name, "why": f"{rev[:12]} → {head[:12]}"})
        except Exception:  # noqa: BLE001 — устаревание не должно ронять проверку
            continue
    return out


def check_story(project: str, story: str) -> dict:
    pack = CP.build_pack(project, story)
    if not pack.get("found"):
        return {"story": story.upper(), "found": False, "criteria": [], "summary": {}, "stale": []}
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
    return {"story": story.upper(), "found": True, "criteria": rows, "summary": summary,
            "stale": _stale(project, indexes)}


AGENT_VERDICTS = ("found", "partial", "not_found", "unsure")
AGENT_PROMPT = (
    "Ты арбитр критериев приёмки. Решай только по перечисленным доказательствам; нельзя "
    "выдумывать файлы и строки. Ответ — JSON: "
    "{\"verdict\":\"found|partial|not_found|unsure\",\"citations\":[\"file:line\"],"
    "\"why\":\"кратко\"}. found допустим только с цитатами из списка."
)


def _coords(evidence: list) -> set:
    return {f"{e.get('file', '')}:{e.get('line', '')}" for e in evidence}


def _agent_messages(criterion: dict) -> list:
    evidence = criterion.get("evidence") or []
    lines = [f"Критерий: {criterion.get('text', '')}",
             f"Детерминированный вердикт: {criterion.get('verdict', '')}",
             f"Не найдено: {', '.join(criterion.get('missing') or []) or 'нет'}",
             "Доказательства:"]
    for e in evidence:
        lines.append(f"- {e.get('file')}:{e.get('line')} · {e.get('kind')} · {e.get('quote')}")
    if not evidence:
        lines.append("- нет")
    return [
        {"role": "system", "content": AGENT_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _parse_answer(answer: dict) -> dict:
    """Ответ модели в одном виде: вердикт, цитаты, причина или ошибка."""
    out = {"ok": False, "verdict": "", "citations": [], "why": "", "error": ""}
    if not answer.get("ok"):
        out["error"] = str(answer.get("error") or "модель не ответила")
        return out
    try:
        data = json.loads(str(answer.get("text") or ""))
        verdict = str(data.get("verdict") or "")
        citations = [str(c) for c in (data.get("citations") or [])]
        why = str(data.get("why") or "")
    except (ValueError, TypeError, AttributeError) as exc:
        out["error"] = f"ответ не JSON: {exc}"
        return out
    if verdict not in AGENT_VERDICTS:
        out["error"] = f"неизвестный вердикт: {verdict}"
        return out
    out.update({"ok": True, "verdict": verdict, "citations": citations, "why": why})
    return out


def _accepts(agent: dict, criterion: dict) -> bool:
    """found принимается только с непустыми цитатами из собранных доказательств."""
    citations = agent.get("citations") or []
    return (agent.get("ok") and agent.get("verdict") == "found" and bool(citations)
            and set(citations) <= _coords(criterion.get("evidence") or []))


def judge(report: dict, ask=None, max_calls: int = 5, deadline: float = 300) -> dict:
    """Модельный арбитр только для спорных критериев; детерминированные факты не трогает."""
    calls = accepted = 0
    rows = []
    for criterion in report.get("criteria") or []:
        base = criterion.get("verdict") or "not_found"
        if base not in ("partial", "not_found"):
            rows.append({**criterion, "final_verdict": base})
            continue
        if calls >= max_calls:
            rows.append({**criterion, "final_verdict": base,
                         "agent": {"skipped": "max_calls"}})
            continue
        calls += 1
        if ask is None:
            import agent_core as AG
            answer = AG.ask_role(_agent_messages(criterion), "worker", deadline, max_tokens=2000)
        else:
            answer = ask(_agent_messages(criterion), deadline)
        agent = _parse_answer(answer)
        agent["accepted"] = _accepts(agent, criterion)
        if agent["accepted"]:
            accepted += 1
        rows.append({**criterion, "agent": agent,
                     "final_verdict": "found" if agent["accepted"] else base})
    summary = dict(report.get("summary") or {})
    summary.update({
        "found": sum(r.get("final_verdict") == "found" for r in rows),
        "partial": sum(r.get("final_verdict") == "partial" for r in rows),
        "not_found": sum(r.get("final_verdict") == "not_found" for r in rows),
        "total": len(rows),
    })
    return {**report, "criteria": rows, "summary": summary,
            "agent": {"calls": calls, "accepted": accepted}}


def render_md(report: dict) -> str:
    lines = [f"# {report['story']}. Проверка критериев по коду", ""]
    if not report.get("found"):
        return "\n".join(lines + ["История не найдена.", ""])
    s = report["summary"]
    lines += [f"Критериев {s['total']}: найдено {s['found']}, частично {s['partial']}, "
              f"не найдено {s['not_found']}. Индекс: {', '.join(s['repos']) or 'нет'}.", ""]
    if report.get("stale"):
        lines.append("⚠ Индекс отстал от клона: " + "; ".join(
            f"{x['repo']} {x['why']}" for x in report["stale"]))
        lines.append("")
    for crit in report["criteria"]:
        verdict = crit.get("final_verdict") or crit["verdict"]
        icon = {"found": "✅", "partial": "◐", "not_found": "·"}[verdict]
        lines.append(f"## {icon} {crit['text']}")
        lines.append("")
        if crit.get("agent", {}).get("accepted"):
            lines.append("Агент: подтвердил found · " + ", ".join(crit["agent"].get("citations") or []))
            lines.append("")
        elif crit.get("agent", {}).get("error"):
            lines.append("Агент: не принят · " + crit["agent"]["error"])
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
    p.add_argument("--fail-on", choices=("none", "partial", "not_found"), default="none",
                   help="код 1 при частичном или ненайденном критерии")
    p.add_argument("--agent", action="store_true",
                   help="модельный арбитр только для спорных критериев")
    p.add_argument("--max-calls", type=int, default=5,
                   help="сколько критериев отдать модели")
    p.add_argument("--deadline", type=float, default=300,
                   help="секунд на один вызов модели")
    a = p.parse_args(argv)
    root = Path(a.project).resolve()
    report = check_story(str(root), a.story)
    if a.agent and report.get("found"):
        report = judge(report, None, max(0, a.max_calls), a.deadline)
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
    threshold = {"none": set(), "partial": {"partial", "not_found"},
                 "not_found": {"not_found"}}[a.fail_on]
    bad = not report.get("found") or any(
        (c.get("final_verdict") or c["verdict"]) in threshold
        for c in report.get("criteria") or [])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
