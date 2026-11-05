#!/usr/bin/env python3
"""clean_room.py — артефакты «как сделано» по коду, детерминированно или моделью.

Читает индекс `code:index`, собирает факты по каждому репозиторию и пишет документы в
`CleanRoom/<вид>/<репозиторий>.md`. Каждое утверждение имеет ссылку `[cN]` на `файл:строка`;
чего нет в индексе, попадает в «Что не удалось установить по коду».

  python3 .aurora/scripts/clean_room.py --apply
  python3 .aurora/scripts/clean_room.py --agent --apply
  python3 .aurora/scripts/clean_room.py --check
  python3 .aurora/scripts/clean_room.py --json

`--agent` — только для `cr-module`, `cr-algorithm`, `cr-rules`: модель получает факты индекса,
обязана ссылаться на них `[cN]` и заполнить раздел «Что не удалось установить по коду».
Нужен `code_repos.<repo>.models: true`; модельный документ пишется только при
`code_repos.<repo>.publish: true`. Без `--agent` модель и сеть не нужны.

Паспорт `CleanRoom/meta/passport.json` хранит ревизию и время; `--check` называет устаревшие
и отсутствующие документы.

Панель: `code:cleanroom`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_artifacts as CA  # noqa: E402
import code_repos as CREPO  # noqa: E402
from aurora_common import TODAY, engine_dir  # noqa: E402
import codeidx.scan as scan  # noqa: E402
import codeidx.store as store_mod  # noqa: E402

OUT = "CleanRoom"
PASSPORT = "CleanRoom/meta/passport.json"
MODEL_KINDS = ("cr-module", "cr-algorithm", "cr-rules")
AGENT_PROMPT = (
    "Ты пишешь документ Clean Room по коду. Опирайся только на перечисленные факты; "
    "нельзя выдумывать файлы, строки, поведение и причины решений. Каждое утверждение "
    "помечай ссылкой [cN] на факт из списка. Ответ — Markdown с двумя обязательными "
    "разделами: «## Что видно по коду» и «## Что не удалось установить по коду». "
    "Во втором разделе честно назови, чего индекс не показывает."
)


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


def _agent_messages(name: str, kind: str, title: str, facts: list) -> list:
    lines = [f"Репозиторий: {name}", f"Вид: {kind} — {title}", "", "Факты индекса:"]
    for i, (label, file, line) in enumerate(facts, 1):
        lines.append(f"[c{i}] {label} · {file}:{line}")
    if not facts:
        lines.append("(индекс не дал фактов этого вида)")
    return [{"role": "system", "content": AGENT_PROMPT},
            {"role": "user", "content": "\n".join(lines)}]


def _cited(text: str) -> list:
    return [int(m.group(1)) for m in re.finditer(r"\[c(\d+)\]", text)]


def synthesize(name: str, kind: str, title: str, facts: list, ask=None,
               deadline: float = 300) -> tuple:
    """Текст модели и ошибка. Годен только с цитатами из фактов и обязательным разделом."""
    messages = _agent_messages(name, kind, title, facts)
    if ask is None:
        import agent_core as AG
        answer = AG.ask_role(messages, "worker", deadline, max_tokens=3000)
    else:
        answer = ask(messages, deadline)
    if not answer.get("ok"):
        return "", str(answer.get("error") or "модель не ответила")
    text = str(answer.get("text") or "").strip()
    if not text or text.startswith("---"):
        return "", "модель вернула пустой ответ или шапку"
    if len(text) > 40000:
        return "", "ответ модели слишком длинный"
    for heading in ("## Что видно по коду", "## Что не удалось установить по коду"):
        if heading not in text:
            return "", f"нет обязательного раздела «{heading[3:]}»"
    tail = text.split("## Что не удалось установить по коду", 1)[1].strip()
    if not tail or tail.startswith("## "):
        return "", "раздел «Что не удалось установить по коду» пуст"
    cites = _cited(text)
    if not cites:
        return "", "в ответе нет ссылок [cN]"
    if any(n < 1 or n > len(facts) for n in cites):
        return "", "ссылка на факт вне списка"
    return text, ""


def _render_agent(name: str, kind: str, title: str, rev: str, facts: list,
                  body: str) -> str:
    head = _render(name, kind, title, rev, facts).split("## Что видно по коду", 1)[0]
    body = body.split("\n## Источники", 1)[0].strip()
    sources = "\n".join(f"- [c{i}] `{file}:{line}`" for i, (_l, file, line) in enumerate(facts, 1))
    return head + body + "\n\n## Источники\n\n" + sources + "\n"


publish_allowed = CREPO.publish_allowed


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


def build(project: str, agent: bool = False, ask=None, deadline: float = 300) -> dict:
    kinds, _problems = CA.check(project)
    enabled = [rec for rec in kinds.values() if rec.get("enabled")]
    indexes = _indexes(project)
    repos = CREPO.read_repos(project)
    docs, blocked = [], []
    for name, st, status in indexes:
        rev = status.get("repo_rev") or status.get("index_rev") or ""
        repo = repos.get(CREPO.find(repos, name)) or {}
        for rec in enabled:
            kind = rec.get("kind") or ""
            facts = _facts(st, kind)
            rel = f"{OUT}/{kind}/{name}.md"
            doc = {"repo": name, "kind": kind, "path": rel, "rev": rev,
                   "title": rec.get("title") or kind, "facts": len(facts),
                   "text": _render(name, kind, rec.get("title") or kind, rev, facts)}
            if agent and kind in MODEL_KINDS:
                allowed, why = publish_allowed(project, name, repo)
                if not allowed:
                    blocked.append({"repo": name, "kind": kind, "why": why})
                    continue
                if not repo.get("models"):
                    doc["agent"] = {"error": f"code_repos.{name}.models: false"}
                else:
                    text, error = synthesize(name, kind, rec.get("title") or kind, facts,
                                             ask, deadline)
                    if error:
                        doc["agent"] = {"error": error}
                    else:
                        doc["text"] = _render_agent(name, kind, rec.get("title") or kind,
                                                    rev, facts, text)
                        doc["agent"] = {"ok": True, "cites": len(set(_cited(text)))}
            docs.append(doc)
    return {"built": TODAY, "docs": docs, "blocked": blocked,
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
        head = ""
        try:
            clone = CREPO.clone_path(project, name)
            if clone.is_dir():
                head = scan.GitRepo(clone).head()
        except Exception:  # noqa: BLE001
            head = ""
        if head and rec.get("rev") and head != rec["rev"]:
            stale.append({"repo": name, "why": f"код {rec['rev'][:12]} → {head[:12]}"})
            continue
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
            stale.append({"repo": name, "why": f"индекс {rec['rev'][:12]} → {rev[:12]}"})
    return {"ok": not missing and not stale, "missing": missing, "stale": stale}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Clean Room: артефакты по коду")
    p.add_argument("--project", default=".", help="корень проекта")
    p.add_argument("--apply", action="store_true", help="записать документы и паспорт")
    p.add_argument("--check", action="store_true", help="проверить паспорт и устаревание")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--agent", action="store_true",
                   help="синтез cr-module/cr-algorithm/cr-rules моделью по фактам индекса")
    p.add_argument("--deadline", type=float, default=300,
                   help="секунд на один вызов модели")
    a = p.parse_args(argv)
    root = str(Path(a.project).resolve())
    if a.check:
        res = check(root)
        print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else
              ("✅ Clean Room актуален" if res["ok"] else
               f"⚠️ отсутствует: {len(res['missing'])} · устарело: {len(res['stale'])}"))
        return 0 if res["ok"] else 1
    data = build(root, agent=a.agent, deadline=a.deadline)
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
    if data["blocked"]:
        for b in data["blocked"]:
            print(f"⛔ {b['repo']}/{b['kind']}: публикация без publish: true ({b['why']})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
