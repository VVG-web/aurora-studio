#!/usr/bin/env python3
"""kb_schema.py — версия схемы карточки и переход между версиями.

Схема менялась трижды за месяц, и каждый раз миграция делалась «по памяти»: кто-то
вспоминал, что поле `audience` больше не читается, и вычищал его grep'ом. Пока в карточке
не написано, по какой схеме она сделана, проверить это невозможно — и невозможно понять,
прошла ли миграция до конца.

  python3 .opencode/scripts/kb_schema.py                 # что в базе: версии и разрывы
  python3 .opencode/scripts/kb_schema.py --apply         # довести карточки до текущей
  python3 .opencode/scripts/kb_schema.py --to 3 --apply  # до конкретной версии

Цепочка переходов объявлена в `MIGRATIONS`: каждая ступень знает, что именно она меняет,
и применяется ровно один раз. Карточка без `schema_version` считается версией 1 — это
исходное состояние легаси-базы, а не ошибка.

Панель: `kb:schema`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (KB_ROOT, RETIRED_FIELDS, RETIRED_STATUS, frontmatter, git_guard,
                           is_service, set_field, split_frontmatter, walk_md)

CURRENT = 4
TODAY = date.today().isoformat()

# Разделы базы → тип карточки: та же таблица, по которой достраивает тип `kb:repair`.
SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Systems": "system",
    "Glossary": "glossary", "Reference": "reference", "Decisions": "decision",
    "Requirements": "requirement", "Specs": "spec", "Questions": "question",
    "Roles": "role", "Statuses": "status-model", "MOC": "moc",
}


def drop_fields(head: str, fields: tuple) -> str:
    return "\n".join(l for l in head.split("\n")
                     if l.split(":", 1)[0].strip() not in fields)


def m2(head: str, section: str) -> tuple:
    """1 → 2: у карточки появились статус доверия, владелец и тип раздела.

    Ступень историческая: `trust` здесь проставляется, а на четвёртой — убирается.
    Переписывать прошлое нельзя: база, стоящая на версии 1, должна пройти тот же путь,
    что прошли остальные.
    """
    changed = []
    fm = frontmatter("---" + head + "\n---\n")
    if not fm.get("status"):
        head = set_field(head, "status", "imported")
        changed.append("status")
    if not fm.get("trust"):
        head = set_field(head, "trust", "medium")
        changed.append("trust")
    if not fm.get("type") and SECTION_TYPE.get(section):
        head = set_field(head, "type", SECTION_TYPE[section])
        changed.append("type")
    return head, changed


def m4(head: str, section: str) -> tuple:
    """3 → 4: поле `trust` выведено из схемы (1.35.0).

    Уровень доверия в базе один и выражается статусом: `imported` — машина принесла,
    `verified` — человек сверил и отвечает. Второе поле писали шесть скриптов, не читал
    ни один, и оно разъезжалось со статусом: `verified` с `trust: medium` — что это значит,
    ответить было нельзя.
    """
    before = head
    head = drop_fields(head, ("trust",))
    return head, (["убрано trust"] if head != before else [])


# Что убрала каждая ступень — записано здесь навсегда. Брать живой список выведенных
# полей нельзя: он растёт, и прошлая ступень начинает делать не то, что делала.
V3_DROPPED = ("audience", "confirmed_by")


def m3(head: str, section: str) -> tuple:
    """2 → 3: ступень `canonical` и поле `audience` выведены из схемы (1.10.0)."""
    changed = []
    before = head
    head = drop_fields(head, V3_DROPPED)
    if head != before:
        changed.append("убраны " + ", ".join(V3_DROPPED))
    m = re.search(r"^status:\s*\"?(\w[\w-]*)", head, re.M)
    if m and m.group(1) in RETIRED_STATUS:
        head = re.sub(r"^status:.*$", f"status: {RETIRED_STATUS[m.group(1)]}", head,
                      count=1, flags=re.M)
        changed.append(f"status {m.group(1)} → {RETIRED_STATUS[m.group(1)]}")
    return head, changed


# версия → (что делает ступень, функция). Ступень применяется ровно один раз.
MIGRATIONS = {
    2: ("статус доверия, trust и тип раздела в каждой карточке", m2),
    3: ("удалены ступень canonical и поле audience", m3),
    4: ("удалено поле trust: доверие выражает status", m4),
}


def card_version(fm: dict) -> int:
    raw = (fm.get("schema_version") or "").strip().strip('"')
    if raw.isdigit():
        return int(raw)
    # без отметки: карточка старше версионирования. Если у неё уже есть статус и тип,
    # значит вторая ступень по ней де-факто прошла — не гоняем её повторно.
    return 2 if fm.get("status") and fm.get("type") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Версия схемы карточек и миграция")
    ap.add_argument("--to", type=int, default=CURRENT, help=f"целевая версия (сейчас {CURRENT})")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (иначе dry-run)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    ap.add_argument("--root", default=KB_ROOT,
                    help="корень базы (по умолчанию AuroraKnowledgeDB)")
    a = ap.parse_args()

    if a.to > CURRENT:
        print(f"kb_schema: версии {a.to} движок не знает, текущая {CURRENT}", file=sys.stderr)
        return 2
    if not os.path.isdir(a.root):
        print(f"kb_schema: нет {a.root}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    by_version: dict = {}
    plan: list = []
    for path in sorted(walk_md(a.root, skip_service=True)):
        text = open(path, encoding="utf-8", errors="ignore").read()
        head, rest = split_frontmatter(text)
        if head is None:
            by_version["без шапки"] = by_version.get("без шапки", 0) + 1
            continue
        fm = frontmatter(text)
        v = card_version(fm)
        by_version[v] = by_version.get(v, 0) + 1
        if v >= a.to:
            continue
        section = os.path.relpath(os.path.dirname(path), a.root).split(os.sep)[0]
        steps = []
        new_head = head
        for step in range(v + 1, a.to + 1):
            if step not in MIGRATIONS:
                continue
            new_head, changed = MIGRATIONS[step][1](new_head, section)
            steps += [f"v{step}: {c}" for c in changed] or [f"v{step}: уже соответствует"]
        new_head = set_field(new_head, "schema_version", str(a.to))
        plan.append((path, "---" + new_head.rstrip("\n") + rest, steps))

    print(f"# Схема карточек — {TODAY}\n")
    print(f"Текущая версия схемы движка: **{CURRENT}**, цель перехода: **{a.to}**\n")
    print("Карточек по версиям:")
    for v in sorted(by_version, key=str):
        mark = " (текущая)" if v == CURRENT else ""
        print(f"  v{v}: {by_version[v]}{mark}" if isinstance(v, int) else f"  {v}: {by_version[v]}")
    print(f"\nК переводу: {len(plan)}")
    if plan:
        print("\nЧто изменится (первые 10):")
        for path, _text, steps in plan[:10]:
            print(f"  {path}")
            for s in steps:
                print(f"      {s}")
    for v, (what, _fn) in sorted(MIGRATIONS.items()):
        print(f"\nv{v - 1} → v{v}: {what}")

    if not plan:
        print("\n✅ Вся база на текущей версии схемы.")
        return 0
    if not a.apply:
        print(f"\n(dry-run) Ничего не записано. Перевести {len(plan)} карточек: --apply")
        return 0
    if not git_guard(a.root, a.allow_dirty, "миграция схемы"):
        return 1
    for path, text, _steps in plan:
        open(path, "w", encoding="utf-8").write(text)
    print(f"\n✅ Переведено карточек: {len(plan)} → schema_version: {a.to}")
    print("   Проверьте `git diff`: в шапке должна появиться отметка версии, тело не тронуто.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
