#!/usr/bin/env python3
"""requirements_check.py — проверка реализованности требований из Markdown по коду.

Сначала разбирает требования из выбранного документа или папки и проверяет каждое по
уже построенным индексам Code тем же детерминированным слоем, что `code:check`.
`--agent` добавляет модельный арбитр для спорных вердиктов: модель видит только
собранные доказательства, не получает код и не может выдумать `file:line`.
Без `--agent` поведение остаётся прежним.

  python3 .aurora/scripts/requirements_check.py --project . --path Artifacts/spec.md
  python3 .aurora/scripts/requirements_check.py --project . --path Artifacts --json
  python3 .aurora/scripts/requirements_check.py --project . --path Artifacts --agent

Панель: `requirements/check`.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aurora_common  # noqa: F401 — защита версии Python до разбора аргументов
import code_check as CC  # noqa: E402

HEAD_RX = re.compile(r"^(#{1,6})\s+(.*)$")
ITEM_RX = re.compile(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)(.+)$")
CHECK_RX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+(.+)$")
TABLE_RX = re.compile(r"^\s*\|(.+)\|\s*$")
REQ_HEAD_RX = re.compile(r"(требован|критери|requirements?|acceptance|спецификац|ac\b)", re.I)
SKIP_HEAD_RX = re.compile(r"(истори|контекст|риск|вопрос|примечан|notes?|links?|источник)", re.I)
MAX_DOCS = 200
REPORT_ROOTS = ("Artifacts/", "ArtifactsCode/", "Development/")


def _clean(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Подчёркивания — часть имён (`order_log`), поэтому снимаем только звёздочки,
    # обратные кавычки и тильду, а не все markdown-символы подряд.
    text = re.sub(r"[*`~]", "", text)
    return " ".join(text.split()).strip()


def parse_requirements(text: str, rel: str = "") -> list:
    """[{id, text, source, line}] из разделов требований, чекбоксов и таблиц."""
    rows, in_req, req_level = [], False, 0
    for n, line in enumerate((text or "").splitlines(), 1):
        head = HEAD_RX.match(line)
        if head:
            title = _clean(head.group(2))
            level = len(head.group(1))
            if REQ_HEAD_RX.search(title):
                in_req, req_level = True, level
            elif in_req and (level <= req_level or SKIP_HEAD_RX.search(title)):
                in_req = False
            continue
        if not in_req:
            continue
        item = ITEM_RX.match(line)
        if item:
            value = _clean(item.group(1))
            if value and "{{" not in value:
                rows.append({"id": f"R-{len(rows) + 1}", "text": value,
                             "source": rel, "line": n})
            continue
        table = TABLE_RX.match(line)
        if table:
            cells = [_clean(c) for c in table.group(1).split("|")]
            if len(cells) >= 2 and cells[0].lower() not in ("№", "#", "номер", "---"):
                value = " — ".join(c for c in cells[:2] if c)
                if value and "{{" not in value and not re.fullmatch(r"[-: ]+", value):
                    rows.append({"id": f"R-{len(rows) + 1}", "text": value,
                                 "source": rel, "line": n})
    if not rows:
        # Документ без заголовка требований, но с чекбоксами: считаем требованиями
        # именно отмеченные пункты, а не весь контекст документа.
        for n, line in enumerate((text or "").splitlines(), 1):
            item = CHECK_RX.match(line)
            value = _clean(item.group(1)) if item else ""
            if value and "{{" not in value:
                rows.append({"id": f"R-{len(rows) + 1}", "text": value,
                             "source": rel, "line": n})
    return rows


def summarize(rows: list) -> dict:
    """Итог по требованиям: found/partial/not_found и процент (partial — половина)."""
    total = len(rows)
    verdicts = [r.get("final_verdict") or r.get("verdict") for r in rows]
    found = sum(v == "found" for v in verdicts)
    partial = sum(v == "partial" for v in verdicts)
    missing = sum(v == "not_found" for v in verdicts)
    score = (found + 0.5 * partial) / total * 100 if total else 0.0
    return {"total": total, "found": found, "partial": partial,
            "not_found": missing, "percent": round(score, 1)}


def _agent_verdict(agent: dict, row: dict) -> bool:
    """Можно ли принять ответ модели.

    `found` — только с цитатами из собранных доказательств; `partial`/`not_found` —
    только с объяснением. `unsure` и отказ модели оставляют детерминированный вердикт.
    """
    if not agent.get("ok") or agent.get("verdict") not in ("found", "partial", "not_found"):
        return False
    citations = agent.get("citations") or []
    allowed = CC._coords(row.get("evidence") or [])
    if agent["verdict"] == "found":
        return bool(citations) and set(citations) <= allowed
    if not str(agent.get("why") or "").strip():
        return False
    return not citations or set(citations) <= allowed


def judge_requirements(result: dict, ask=None, max_calls: int = 10,
                       deadline: float = 300) -> dict:
    """Модельный арбитр спорных требований.

    Детерминированный `found` не трогаем. Для `partial`/`not_found` модель получает
    только доказательства и может подтвердить `found` с цитатами либо уточнить
    `partial`/`not_found` с объяснением. Ошибка модели не отменяет детерминированный
    отчёт: строка остаётся с исходным вердиктом и причиной отказа.
    """
    rows = []
    for di, doc in enumerate(result.get("documents") or []):
        for ri, row in enumerate(doc.get("requirements") or []):
            rows.append({**row, "_doc": di, "_row": ri})
    calls = accepted = errors = skipped = 0
    for row in rows:
        base = row.get("verdict") or "not_found"
        row["final_verdict"] = base
        if base == "found":
            continue
        if calls >= max(0, int(max_calls)):
            row["agent"] = {"skipped": "max_calls"}
            skipped += 1
            continue
        calls += 1
        messages = CC._agent_messages({**row, "verdict": base})
        if ask is None:
            import agent_core as AG
            answer = AG.ask_role(messages, "worker", deadline, max_tokens=2000)
        else:
            answer = ask(messages, deadline)
        agent = CC._parse_answer(answer)
        agent["accepted"] = _agent_verdict(agent, row)
        row["agent"] = agent
        if agent.get("accepted"):
            accepted += 1
            row["final_verdict"] = agent["verdict"]
        elif not agent.get("ok"):
            errors += 1
    for row in rows:
        doc = (result.get("documents") or [])[row["_doc"]]
        target = (doc.get("requirements") or [])[row["_row"]]
        target.update({k: v for k, v in row.items() if k not in ("_doc", "_row")})
    for doc in result.get("documents") or []:
        doc["summary"] = summarize(doc.get("requirements") or [])
    all_rows = [r for d in result.get("documents") or [] for r in d.get("requirements") or []]
    result["summary"] = summarize(all_rows)
    result["agent"] = {"calls": calls, "accepted": accepted,
                       "skipped": skipped, "errors": errors}
    return result


def _paths(project: str, raw_paths) -> list:
    root = Path(project).resolve()
    out, seen = [], set()
    for raw in raw_paths or []:
        if not str(raw).strip():
            continue
        path = Path(str(raw))
        path = path if path.is_absolute() else root / path
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        files = sorted(path.rglob("*.md")) if path.is_dir() else [path]
        for file in files:
            if not file.is_file() or file in seen or len(out) >= MAX_DOCS:
                continue
            seen.add(file)
            out.append(file)
    return out


def check(project: str, paths, agent: bool = False, max_calls: int = 10,
          deadline: float = 300, ask=None) -> dict:
    """Проверить документы/папки по индексам Code; без индексов — not_found, не падение."""
    root = Path(project).resolve()
    index_error = ""
    try:
        indexes = CC._indexes(str(root))
    except Exception as e:  # noqa: BLE001 — сломанный индекс не должен стирать сам отчёт
        indexes, index_error = [], f"{type(e).__name__}: {e}"
    docs = []
    for path in _paths(str(root), paths):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            docs.append({"path": rel, "title": path.stem, "error": str(e),
                         "requirements": [], "summary": summarize([])})
            continue
        reqs = parse_requirements(text, rel)
        rows = []
        for req in reqs:
            evidence = CC._evidence_for({"text": req["text"], "tokens": []}, indexes)
            missing = CC._missing({"text": req["text"], "tokens": []}, evidence)
            verdict = "found" if evidence and not missing else "partial" if evidence else "not_found"
            rows.append({**req, "verdict": verdict, "evidence": evidence[:6], "missing": missing})
        title = next((_clean(m.group(2)) for m in map(HEAD_RX.match, text.splitlines())
                      if m and m.group(1) == "#"), path.stem)
        docs.append({"path": rel, "title": title, "requirements": rows,
                     "summary": summarize(rows)})
    all_rows = [r for d in docs for r in d.get("requirements") or []]
    result = {"documents": docs, "summary": summarize(all_rows),
              "indexed_repos": [name for name, _ in indexes], "index_error": index_error}
    if agent:
        result = judge_requirements(result, ask, max_calls, deadline)
    return result


def _report_path(project: str, out: str = "") -> Path:
    """Куда писать отчёт: только внутрь проекта и только в разрешённые папки."""
    root = Path(project).resolve()
    if out:
        rel = str(out).strip().replace("\\", "/")
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".md":
            raise ValueError("отчёт: нужен относительный .md-путь внутри проекта")
        if not any(rel.startswith(prefix) for prefix in REPORT_ROOTS):
            raise ValueError("отчёт: только Artifacts/, ArtifactsCode/ или Development/")
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError as e:
            raise ValueError("отчёт: путь выходит за пределы проекта") from e
        return target
    base = root / "ArtifactsCode" / "requirements"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    target = base / f"{stamp}.md"
    n = 2
    while target.exists():
        target = base / f"{stamp}-{n}.md"
        n += 1
    return target


def write_report(project: str, paths, out: str = "", agent: bool = False,
                 max_calls: int = 10, deadline: float = 300, ask=None) -> dict:
    """Проверить требования и сохранить Markdown-отчёт; документы не меняются."""
    result = check(project, paths, agent, max_calls, deadline, ask)
    target = _report_path(project, out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_md(result), encoding="utf-8")
    return {"ok": True, "path": target.relative_to(Path(project).resolve()).as_posix(),
            "summary": result["summary"]}


def render_md(result: dict) -> str:
    s = result["summary"]
    lines = ["# Проверка требований по коду", "",
             f"Требований: {s['total']}; найдено: {s['found']}; частично: {s['partial']}; "
             f"не найдено: {s['not_found']}; итог: {s['percent']}%.", ""]
    if result.get("index_error"):
        lines += [f"Индекс Code не прочитался: {result['index_error']}", ""]
    if result.get("agent"):
        ag = result["agent"]
        lines += [f"Модель: вызовов {ag.get('calls', 0)}; принято {ag.get('accepted', 0)}; "
                  f"пропущено {ag.get('skipped', 0)}; ошибок {ag.get('errors', 0)}.", ""]
    for doc in result.get("documents") or []:
        lines += [f"## {doc.get('title') or doc['path']}", ""]
        lines += [f"Источник: `{doc['path']}`", ""]
        if doc.get("error"):
            lines += [f"Ошибка чтения: {doc['error']}", ""]
            continue
        lines += ["| # | Требование | Вердикт | Где | Комментарий |", "|---|---|---|---|---|"]
        for row in doc.get("requirements") or []:
            where = ", ".join(f"`{e['file']}:{e['line']}`" for e in (row.get("evidence") or [])[:2])
            text = str(row["text"]).replace("|", "\\|")
            missing = ", ".join(row.get("missing") or []).replace("|", "\\|") or "—"
            verdict = row.get("final_verdict") or row.get("verdict") or "not_found"
            agent = row.get("agent") or {}
            note = ""
            if agent.get("accepted"):
                note = "LLM: " + str(agent.get("why") or agent.get("verdict") or "")
            elif agent.get("error"):
                note = "LLM: не принят, " + str(agent.get("error") or "")
            elif agent.get("skipped"):
                note = "LLM: пропущен, " + str(agent.get("skipped"))
            comment = missing + (("; " + note) if note else "")
            lines.append(f"| {row['id']} | {text} | {verdict} | {where or '—'} | {comment} |")
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Проверка реализованности требований по коду")
    ap.add_argument("--project", default=".", help="корень проекта")
    ap.add_argument("--path", action="append", default=[], help="документ или папка (можно несколько)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--agent", action="store_true",
                    help="модельный арбитр для спорных требований")
    ap.add_argument("--max-calls", type=int, default=10,
                    help="сколько спорных требований отдать модели")
    ap.add_argument("--deadline", type=float, default=300,
                    help="секунд на один вызов модели")
    a = ap.parse_args(argv)
    if a.out:
        try:
            res = write_report(a.project, a.path, a.out, a.agent, a.max_calls, a.deadline)
        except (OSError, ValueError) as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
        print(f"Записано: {res['path']}")
        return 0
    result = check(a.project, a.path, a.agent, a.max_calls, a.deadline)
    text = json.dumps(result, ensure_ascii=False, indent=1) if a.json else render_md(result)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
