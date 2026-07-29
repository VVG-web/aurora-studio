#!/usr/bin/env python3
"""kb_supersede.py — замена знания без потери истории (фреймворк «Аврора»).

Инвариант 2: ничего не удаляем. Устаревшая карточка получает `deprecated`,
`superseded_by`, запись в «## История» и переезжает в `_archive/`; преемник получает
`supersedes`; все входящие ссылки переписываются на преемника — иначе verified-карточки
начинают ссылаться на устаревшее (инвариант frontmatter).

Всё перечисленное — механика, решение «это устарело» принимает человек.

  python3 .opencode/scripts/kb_supersede.py <старая> <преемник>          # что будет сделано
  python3 .opencode/scripts/kb_supersede.py <старая> <преемник> --apply
  python3 .opencode/scripts/kb_supersede.py <старая> <преемник> --dr DR-0007-выбор-шины --apply

Имена — как в wiki-ссылках (без .md). Преемник должен существовать: замена «в никуда»
оставила бы базу с deprecated-карточкой и битой ссылкой.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import date

from aurora_common import frontmatter, rewrite_links, set_field, split_frontmatter

ROOT = "AuroraKnowledgeDB"
ARCHIVE = os.path.join(ROOT, "_archive")
TODAY = date.today().isoformat()


def find_card(stem: str) -> str | None:
    for dirpath, _, files in os.walk(ROOT):
        for f in files:
            if f == f"{stem}.md":
                return os.path.join(dirpath, f).replace("\\", "/")
    return None


def append_history(body: str, line: str) -> str:
    if "## История" in body:
        return re.sub(r"(## История\s*\n)", r"\1\n" + line + "\n", body, count=1)
    return body.rstrip("\n") + f"\n\n## История\n\n{line}\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Заменить знание, сохранив историю")
    ap.add_argument("old", help="устаревшая карточка (имя без .md)")
    ap.add_argument("new", help="карточка-преемник (имя без .md)")
    ap.add_argument("--dr", help="Decision Record, обосновывающий замену")
    ap.add_argument("--reason", default="", help="короткая причина для «## История»")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (иначе dry-run)")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"kb_supersede: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    old_path, new_path = find_card(a.old), find_card(a.new)
    if not old_path:
        print(f"kb_supersede: не найдена карточка {a.old}", file=sys.stderr)
        return 1
    if not new_path:
        print(f"kb_supersede: не найден преемник {a.new} — замена «в никуда» оставит битую ссылку",
              file=sys.stderr)
        return 1
    if os.path.abspath(old_path) == os.path.abspath(new_path):
        print("kb_supersede: карточка не может заменить саму себя", file=sys.stderr)
        return 1

    writes, moves, relinked = {}, [], []
    reason = a.reason or "заменена преемником"
    dr_note = f" (см. [[{a.dr}]])" if a.dr else ""

    # 1. устаревшая → deprecated + superseded_by + история
    text = open(old_path, encoding="utf-8").read()
    head, rest = split_frontmatter(text)
    if head is None:
        print(f"kb_supersede: у {a.old} нет frontmatter — сначала приведите карточку в схему "
              "(kb_fix.py --frontmatter)", file=sys.stderr)
        return 1
    head = set_field(head, "status", "deprecated")
    head = set_field(head, "superseded_by", f'"[[{a.new}]]"')
    head = set_field(head, "updated", TODAY)
    body = append_history(rest, f"- {TODAY}: {reason} → [[{a.new}]]{dr_note}.")
    writes[old_path] = "---" + head + body
    if "/_archive/" not in old_path:
        moves.append((old_path, os.path.join(ARCHIVE, os.path.basename(old_path)).replace("\\", "/")))

    # 2. преемник → supersedes + история
    ntext = open(new_path, encoding="utf-8").read()
    nhead, nrest = split_frontmatter(ntext)
    if nhead is not None:
        cur = re.search(r"^supersedes:\s*(.*)$", nhead, re.M)
        items = cur.group(1).strip("[] ") if cur else ""
        merged = ", ".join(x for x in [items, f'"[[{a.old}]]"'] if x)
        nhead = set_field(nhead, "supersedes", f"[{merged}]")
        nhead = set_field(nhead, "updated", TODAY)
        nrest = append_history(nrest, f"- {TODAY}: заменяет [[{a.old}]]{dr_note}.")
        writes[new_path] = "---" + nhead + nrest

    # 3. входящие ссылки → на преемника
    for dirpath, _, files in os.walk(ROOT):
        for f in files:
            if not f.endswith(".md"):
                continue
            p = os.path.join(dirpath, f).replace("\\", "/")
            if p in (old_path, new_path):
                continue
            t = open(p, encoding="utf-8", errors="ignore").read()
            nt = rewrite_links(t, {a.old: a.new})
            if nt != t:
                writes[p] = nt
                relinked.append(p)

    print(f"# Supersede — {TODAY}\n")
    print(f"{a.old} → {a.new}" + (f" · DR: {a.dr}" if a.dr else ""))
    print(f"- устаревшая: deprecated + superseded_by, переезд в _archive/")
    print(f"- преемник: supersedes + запись в «История»")
    print(f"- входящих ссылок переписано: {len(relinked)}")
    for p in relinked[:15]:
        print(f"    {p}")
    if len(relinked) > 15:
        print(f"    … ещё {len(relinked) - 15}")

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    for p, t in writes.items():
        open(p, "w", encoding="utf-8").write(t)
    for src, dst in moves:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            print(f"  ! в _archive уже есть {os.path.basename(dst)} — файл оставлен на месте",
                  file=sys.stderr)
            continue
        shutil.move(src, dst)
    print(f"\n✅ Готово. Проверьте: kb_lint.py --summary && git diff --stat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
