#!/usr/bin/env python3
"""kb_trace_table.py — таблица трассировки: артефакт ↔ задачи (фреймворк «Аврора»).

Доверие в Авроре — свойство источника, а не карточки, и вычисляется, а не присваивается.
Вычислять его можно, только зная, с какими задачами связан артефакт: статус задачи и
решает, устоялась постановка или ещё меняется.

  python3 .opencode/scripts/kb_trace_table.py            # что получилось
  python3 .opencode/scripts/kb_trace_table.py --apply    # записать таблицу

Связи бывают двух родов.

**Прямая** — одна из двух:
  • номер совпал: `AC-10.3.1` в заголовке артефакта и `US-10.3.1` в Summary задачи.
    Сравнение по границе токена: `10.3.11` — уже другой номер, и это не придирка, а
    единственный способ не склеить две соседние истории;
  • ссылка видна хотя бы с одной стороны: ключ задачи в тексте артефакта, URL страницы
    или её `page_id` в задаче.

**Косвенная** — трассировка через артефакты, глубиной до двух переходов: артефакт → другой
артефакт → третий, у которого есть прямая связь. Дальше связь размывается настолько, что
доверять ей нельзя: через три перехода в большой базе связано всё со всем.

Таблица лежит в `AuroraKnowledgeDB/meta/trace/` — в базе, а не в `Sources/`: зеркало
перезаписывает синк и чистит `--prune`, и таблица жила бы там до первой уборки. Рядом
человекочитаемый свод `MOC/Трассировка.md`: это надо уметь открыть в Obsidian, а не только
скормить скрипту.

Панель: `ops:trace-table`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter, walk_md  # noqa: E402

try:
    import sources_registry as REG
except Exception:                                    # noqa: BLE001
    REG = None

TODAY = date.today().isoformat()
OUT_DIR = os.path.join("AuroraKnowledgeDB", "meta", "trace")
TABLE = os.path.join(OUT_DIR, "trace.json")
MOC = os.path.join("AuroraKnowledgeDB", "MOC", "Трассировка.md")
DEPTH = 2                       # переходов по артефактам: дальше связь ничего не значит

# Номер истории: префикс при сравнении отбрасывается, значим только сам номер.
NUM = re.compile(r"(?i)\b(?:US|AC|ALG|SPEC|REQ)?[\s._-]?(\d+(?:\.\d+){1,3})\b")
KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
PAGE_ID = re.compile(r"\b(?:pageId|page_id)[=:\s\"']*(\d{4,})", re.I)


def unq(v: str) -> str:
    return (v or "").strip().strip('"\'')


def mirrors(root: str = ".") -> dict:
    """{путь: роль}. Реестра нет — работаем по историческим именам зеркал."""
    # Исторические умолчания: проект мог не успеть обновить описания коннекторов, а роль
    # у зеркала JIRA всегда была одна. Объявленное всегда сильнее умолчания.
    out = {"Sources/JIRA": "tasks", "Sources/Confluence": "artifacts"}
    if REG is not None:
        try:
            out.update(REG.roles(root))
        except Exception:                            # noqa: BLE001
            pass
    return out


def read_side(path: str) -> dict:
    text = open(path, encoding="utf-8", errors="ignore").read()
    fm = frontmatter(text)
    return {"path": path.replace("\\", "/"), "fm": fm, "text": text}


def numbers(s: str) -> set:
    return {m.group(1) for m in NUM.finditer(s or "")}


def collect(root: str = ".") -> tuple:
    """(задачи, артефакты) по ролям зеркал."""
    tasks, arts = [], []
    for base, role in mirrors(root).items():
        full = os.path.join(root, base)
        if not os.path.isdir(full):
            continue
        for p in walk_md(full):
            side = read_side(p)
            # Путь в таблице — как в карточке: относительно корня проекта. Иначе `./` от
            # обхода не совпадёт с `source:` карточки, и таблица окажется бесполезной,
            # оставаясь при этом внешне правильной.
            rel = os.path.relpath(p, root).replace("\\", "/")
            side["path"] = rel
            side["mirror"] = base
            (tasks if role == "tasks" else arts).append(side)
    return tasks, arts


def direct(tasks: list, arts: list) -> dict:
    """{артефакт: [(ключ задачи, чем доказано)]} — только прямые связи.

    Каждая связь записывается вместе с доказательством: через месяц никто не воспроизведёт
    по памяти, почему карточка оказалась доверенной, а по строке «номер 10.3.1 в заголовке»
    — воспроизведёт за секунду.
    """
    by_num: dict = {}
    by_key: dict = {}
    by_page: dict = {}
    for t in tasks:
        key = unq(t["fm"].get("key")) or os.path.splitext(os.path.basename(t["path"]))[0]
        by_key[key] = t
        for n in numbers(unq(t["fm"].get("title"))):
            by_num.setdefault(n, []).append((key, n))
        blob = t["text"]
        for m in PAGE_ID.finditer(blob):
            by_page.setdefault(m.group(1), []).append(key)
    out: dict = {}
    for a in arts:
        rel = a["path"]
        title = unq(a["fm"].get("title")) or os.path.splitext(os.path.basename(rel))[0]
        found = {}
        for n in numbers(title):
            for key, num in by_num.get(n, []):
                found[key] = f"номер {num} в заголовке артефакта и в задаче"
        for m in KEY.finditer(a["text"]):
            if m.group(1) in by_key:
                found.setdefault(m.group(1), f"ключ {m.group(1)} в тексте артефакта")
        pid = unq(a["fm"].get("page_id")) or unq(a["fm"].get("id"))
        for key in by_page.get(pid, []):
            found.setdefault(key, f"page_id {pid} в задаче")
        if found:
            out[rel] = sorted(found.items())
    return out


def art_links(arts: list) -> dict:
    """{артефакт: {артефакты, на которые он ссылается}} — по именам файлов зеркала."""
    by_name = {os.path.splitext(os.path.basename(a["path"]))[0]: a["path"] for a in arts}
    out = {a["path"]: set() for a in arts}
    for a in arts:
        for name, path in by_name.items():
            if path != a["path"] and name and name in a["text"]:
                # Связь считается в обе стороны. «Алгоритм упомянут в критериях приёмки»
                # и «критерии упоминают алгоритм» — одно и то же отношение, записанное с
                # разных концов; направление ссылки в вики говорит о том, кто писал текст,
                # а не о том, что от чего зависит.
                out[a["path"]].add(path)
                out.setdefault(path, set()).add(a["path"])
    return out


def indirect(direct_map: dict, links: dict, depth: int = DEPTH) -> dict:
    """{артефакт: [(ключ, путь трассировки, глубина)]} — связь через соседей."""
    out: dict = {}
    for start in links:
        if start in direct_map:
            continue
        seen, front, found = {start}, [(start, [start])], {}
        for step in range(1, depth + 1):
            nxt = []
            for node, path in front:
                for nb in links.get(node, ()):
                    if nb in seen:
                        continue
                    seen.add(nb)
                    trail = path + [nb]
                    if nb in direct_map:
                        for key, _why in direct_map[nb]:
                            found.setdefault(key, (trail, step))
                    else:
                        nxt.append((nb, trail))
            front = nxt
            if not front:
                break
        if found:
            out[start] = [(k, [os.path.basename(x) for x in tr], d)
                          for k, (tr, d) in sorted(found.items())]
    return out


def build(root: str = ".") -> dict:
    tasks, arts = collect(root)
    dmap = direct(tasks, arts)
    imap = indirect(dmap, art_links(arts))
    return {"date": TODAY, "tasks": len(tasks), "artifacts": len(arts),
            "direct": {k: [{"key": key, "why": why} for key, why in v]
                       for k, v in dmap.items()},
            "indirect": {k: [{"key": key, "trail": tr, "depth": d} for key, tr, d in v]
                         for k, v in imap.items()}}


def render_moc(t: dict) -> str:
    # Шапка первой строкой: файл, который начинается не с «---», для движка не карточка,
    # и его frontmatter не читается вовсе. Пометка генерации — сразу под шапкой.
    L = ["---", 'title: "Трассировка"', "type: moc", "status: index", "kind: dictionary",
         f"updated: {TODAY}", "---", "",
         "<!-- ФАЙЛ ГЕНЕРИРУЕТСЯ kb_trace_table.py — ручные правки будут потеряны. -->", "", "# Трассировка: артефакт → задача", "",
         f"_Задач: {t['tasks']} · артефактов: {t['artifacts']} · прямых связей: "
         f"{len(t['direct'])} · косвенных: {len(t['indirect'])} · собрано {t['date']}_", "",
         "Прямая связь — совпавший номер или ссылка. Косвенная — трассировка через "
         "артефакты, до двух переходов. Класс доверия карточки считается по этой таблице: "
         "`kb:trust`.", "", "## Прямые связи", "", "| Артефакт | Задачи | Чем доказано |",
         "|---|---|---|"]
    for path, rows in sorted(t["direct"].items())[:400]:
        keys = ", ".join(r["key"] for r in rows[:4])
        L.append(f"| {os.path.basename(path)} | {keys} | {rows[0]['why']} |")
    L += ["", "## Косвенные связи (трассировка)", "",
          "| Артефакт | Задачи | Путь | Глубина |", "|---|---|---|---|"]
    for path, rows in sorted(t["indirect"].items())[:400]:
        r = rows[0]
        L.append(f"| {os.path.basename(path)} | "
                 f"{', '.join(x['key'] for x in rows[:4])} | "
                 f"{' → '.join(r['trail'])} | {r['depth']} |")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Таблица трассировки: артефакты и задачи")
    ap.add_argument("--apply", action="store_true", help="записать таблицу и свод")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    if not os.path.isdir(os.path.join(a.root, "Sources")):
        print("kb_trace_table: нет Sources/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    t = build(a.root)
    print(f"# Трассировка — {TODAY}\n")
    print(f"Задач в зеркале: {t['tasks']} · артефактов: {t['artifacts']}")
    print(f"Артефактов с прямой связью: {len(t['direct'])}")
    print(f"Артефактов со связью через трассировку: {len(t['indirect'])}")
    orphan = t["artifacts"] - len(t["direct"]) - len(t["indirect"])
    print(f"Без связи с задачами: {orphan} — их класс доверия будет «unknown»")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    os.makedirs(os.path.join(a.root, OUT_DIR), exist_ok=True)
    Path(a.root, TABLE).write_text(json.dumps(t, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    os.makedirs(os.path.dirname(os.path.join(a.root, MOC)), exist_ok=True)
    Path(a.root, MOC).write_text(render_moc(t), encoding="utf-8")
    print(f"\n✅ Таблица: {TABLE} · свод: {MOC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
