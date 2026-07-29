#!/usr/bin/env python3
"""kb_impact.py — обратная трассировка по графу базы (фреймворк «Аврора»).

Два вопроса, на которые схема отвечала только в теории:

  «Что устареет, если эта карточка изменилась?»   → kb_impact.py <карточка>
  «На чём собран этот документ и чему он верит?»  → kb_impact.py --explain <файл>

Обход графа — механика: ребро это wiki-ссылка или запись в `based_on`. Опасность,
которую видно только так: **сданный заказчику документ, собранный на карточке, которая
изменилась или оказалась непроверенной**.

  python3 .opencode/scripts/kb_impact.py Основной-объект
  python3 .opencode/scripts/kb_impact.py --explain Deliverables/work/ОПЗ_v1.md
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

from aurora_common import TRUSTED, as_list, frontmatter, link_targets, walk_md

ROOT = "AuroraKnowledgeDB"
PRODUCTS = ["Artifacts", "Deliverables"]
TODAY = date.today().isoformat()






def scan(paths: list) -> dict:
    """{путь: (frontmatter, ссылки, based_on)} по всем markdown-файлам указанных корней."""
    out = {}
    for root in paths:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if not f.endswith(".md"):
                    continue
                p = os.path.join(dirpath, f).replace("\\", "/")
                try:
                    text = open(p, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                fm = frontmatter(text)
                links = set(link_targets(text))
                out[p] = (fm, links, as_list(fm.get('based_on', '')))
    return out


def impact(target: str) -> int:
    graph = scan([ROOT] + PRODUCTS)
    hit = [p for p in graph if os.path.splitext(os.path.basename(p))[0] == target]
    if not hit:
        print(f"kb_impact: карточка {target} не найдена", file=sys.stderr)
        return 1
    card_fm = graph[hit[0]][0]

    cards, artifacts, delivered, released = [], [], [], []
    for p, (fm, links, base) in graph.items():
        if p in hit:
            continue
        touched = target in links or target in base
        if not touched:
            continue
        how = "based_on" if target in base else "ссылка"
        if p.startswith(ROOT):
            cards.append((p, how, (fm.get("status") or "").strip()))
        elif p.startswith("Deliverables/released"):
            released.append((p, how))
        elif p.startswith("Deliverables"):
            delivered.append((p, how))
        else:
            artifacts.append((p, how))

    print(f"# Влияние карточки {target} — {TODAY}\n")
    print(f"Статус карточки: {card_fm.get('status', '—')} · владелец {card_fm.get('owner', '—')} "
          f"· проверено {card_fm.get('verified', '—')}\n")
    print(f"Зависит от неё: карточек {len(cards)}, артефактов {len(artifacts)}, "
          f"поставляемых документов {len(delivered)}, **сданных {len(released)}**\n")

    if released:
        print("## ⚠️ Сданные заказчику документы\n")
        print("Их изменить нельзя (инвариант 6). Если знание изменилось — это не правка файла,")
        print("а решение: выпустить новую версию документа или зафиксировать расхождение.\n")
        for p, how in released:
            print(f"- {p} ({how})")
        print()
    for title, rows in (("Поставляемые документы (work)", delivered),
                        ("Артефакты", artifacts)):
        if rows:
            print(f"## {title}\n")
            for p, how in rows[:30]:
                print(f"- {p} ({how})")
            if len(rows) > 30:
                print(f"- … ещё {len(rows) - 30}")
            print()
    if cards:
        print("## Карточки базы\n")
        for p, how, st in cards[:40]:
            mark = ""
            print(f"- {p} ({how}, {st or 'без статуса'}){mark}")
        if len(cards) > 40:
            print(f"- … ещё {len(cards) - 40}")
    return 0


def explain(path: str) -> int:
    if not os.path.isfile(path):
        print(f"kb_impact: нет файла {path}", file=sys.stderr)
        return 1
    text = open(path, encoding="utf-8", errors="ignore").read()
    fm = frontmatter(text)
    base = as_list(fm.get('based_on', ''))
    links = set(link_targets(text))
    cards = scan([ROOT])
    by_stem = {os.path.splitext(os.path.basename(p))[0]: (p, f) for p, (f, _, _) in cards.items()}

    print(f"# На чём собран {path} — {TODAY}\n")
    print(f"Тип: {fm.get('type', '—')} · версия: {fm.get('version', '—')} "
          f"· передан: {fm.get('released', '—')}\n")
    if not base:
        print("⚠️ `based_on` пуст — документ непрослеживаем: неизвестно, из какого знания он собран.")
        print("   `assemble`/`create` обязаны его заполнять.\n")

    rows, weak, missing = [], [], []
    for stem in base or sorted(links & set(by_stem)):
        item = by_stem.get(stem)
        if not item:
            missing.append(stem)
            continue
        p, f = item
        st = (f.get("status") or "").strip()
        rows.append((stem, st, f.get("verified", "—"), f.get("review_by", "—"), p))
        if st not in TRUSTED:
            weak.append((stem, st or "без статуса"))

    if rows:
        print("| Карточка | Статус | Проверено | Годно до |")
        print("|---|---|---|---|")
        for stem, st, ver, rb, _ in rows:
            expired = " ⚠️просрочено" if rb and rb != "—" and rb < TODAY else ""
            print(f"| {stem} | {st or '—'} | {ver} | {rb}{expired} |")
        print()
    if weak:
        print(f"⚠️ Оснований ниже verified: {len(weak)} — "
              f"{', '.join(f'{s} ({st})' for s, st in weak[:8])}")
        print("   Документ собран на непроверенном знании; для сданного это риск приёмки.\n")
    if missing:
        print(f"⚠️ В `based_on` есть карточки, которых нет в базе: {', '.join(missing[:8])}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Обратная трассировка по графу базы знаний")
    ap.add_argument("target", nargs="?", help="карточка (имя без .md)")
    ap.add_argument("--explain", metavar="FILE", help="документ: на чём он собран")
    a = ap.parse_args()
    if not os.path.isdir(ROOT):
        print(f"kb_impact: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    if a.explain:
        return explain(a.explain)
    if not a.target:
        ap.print_help()
        return 0
    return impact(a.target)


if __name__ == "__main__":
    sys.exit(main())
