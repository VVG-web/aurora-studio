#!/usr/bin/env python3
"""kb_queue.py — очередь верификации AuroraKnowledgeDB (фреймворк «Аврора»).

Проблема, которую решает: в живой базе тысячи карточек со `status: imported`, и
верифицировать их подряд невозможно — bootstrap-режим становится вечным. Верифицировать
надо то, что реально работает: попадает в context pack, на что ссылаются другие карточки
и на чём собраны артефакты и поставляемые документы.

Скрипт считает ценность каждой непроверенной карточки и печатает очередь:

  вес = 3×употребления в context pack   (AuroraKnowledgeDB/meta/usage.log)
      + 2×входящие ссылки из базы знаний
      + 4×использование в продуктах     (Artifacts/, Deliverables/, Specs/ — based_on и ссылки)
      + 2×справочник/глоссарий          (термины подмешиваются в каждый пак)

Плюс отдельная секция «протухшие» — verified-карточки с истёкшим review_by.

Запуск из корня проекта:
  python3 .opencode/scripts/kb_queue.py                  # топ-30 к верификации
  python3 .opencode/scripts/kb_queue.py --limit 100 --theme Glossary
  python3 .opencode/scripts/kb_queue.py --report Artifacts/reports/2026-07-26_queue.md

Ничего не пишет в базу: это отчёт для команды `verify`.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date

from aurora_common import TRUSTED, frontmatter, link_targets

ROOT = "AuroraKnowledgeDB"
USAGE = os.path.join(ROOT, "meta", "usage.log")
PRODUCT_DIRS = ["Artifacts", "Deliverables", os.path.join(ROOT, "Specs")]
UNVERIFIED = {"", "imported", "draft", "in-review"}
TODAY = date.today().isoformat()

W_USAGE, W_INBOUND, W_PRODUCT, W_REFERENCE = 3, 2, 4, 2




def load_cards(root: str) -> dict:
    cards = {}
    for dirpath, _, files in os.walk(root):
        if "/_archive" in dirpath.replace("\\", "/"):
            continue
        for f in files:
            if not f.endswith(".md") or f.startswith("_") or f == "index.md":
                continue
            p = os.path.join(dirpath, f).replace("\\", "/")
            if "/meta/" in p:
                continue
            try:
                text = open(p, encoding="utf-8").read()
            except Exception:
                continue
            cards[p] = {"stem": f[:-3], "fm": frontmatter(text), "text": text,
                        "section": os.path.relpath(dirpath, root).split(os.sep)[0]}
    return cards


def count_links(paths_texts, stems: set) -> Counter:
    hits = Counter()
    for text in paths_texts:
        for leaf in link_targets(text):
            if leaf in stems:
                hits[leaf] += 1
    return hits


def product_texts() -> list:
    out = []
    for d in PRODUCT_DIRS:
        if not os.path.isdir(d):
            continue
        for dirpath, _, files in os.walk(d):
            for f in files:
                if f.endswith(".md"):
                    try:
                        out.append(open(os.path.join(dirpath, f), encoding="utf-8").read())
                    except Exception:
                        pass
    return out


def read_usage() -> Counter:
    """usage.log: строки «YYYY-MM-DD<TAB>команда<TAB>карточка». Пишется retrieval-политикой."""
    c = Counter()
    if not os.path.isfile(USAGE):
        return c
    for line in open(USAGE, encoding="utf-8", errors="ignore"):
        parts = [p.strip() for p in line.strip().split("\t")]
        if len(parts) >= 3 and parts[2]:
            c[os.path.splitext(os.path.basename(parts[2]))[0]] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description="Очередь верификации карточек по реальной ценности")
    ap.add_argument("--limit", type=int, default=30, help="сколько карточек показать (по умолчанию 30)")
    ap.add_argument("--theme", help="фильтр по разделу или подстроке имени (напр. Glossary, Заявка)")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт в файл")
    ap.add_argument("--root", default=ROOT,
                    help="корень базы (по умолчанию AuroraKnowledgeDB)")
    a = ap.parse_args()

    if not os.path.isdir(a.root):
        print(f"kb_queue: нет папки {a.root}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    cards = load_cards(a.root)
    stems = {c["stem"] for c in cards.values()}
    inbound = count_links((c["text"] for c in cards.values()), stems)
    in_products = count_links(product_texts(), stems)
    usage = read_usage()

    scored, expired, by_status = [], [], Counter()
    for path, c in cards.items():
        status = (c["fm"].get("status") or "").strip()
        by_status[status or "(нет status)"] += 1
        stem, section = c["stem"], c["section"]
        if status == "verified":
            rb = (c["fm"].get("review_by") or "").strip()
            if rb and rb < TODAY:
                expired.append((rb, path, c["fm"].get("owner", "—")))
            continue
        if status not in UNVERIFIED:
            continue
        score = (W_USAGE * usage.get(stem, 0)
                 + W_INBOUND * inbound.get(stem, 0)
                 + W_PRODUCT * in_products.get(stem, 0)
                 + (W_REFERENCE if section in ("Reference", "Glossary") else 0))
        if score <= 0:
            continue
        if a.theme and a.theme.lower() not in (section + " " + stem).lower():
            continue
        scored.append({"score": score, "path": path, "stem": stem, "section": section,
                       "status": status or "(нет)", "usage": usage.get(stem, 0),
                       "inbound": inbound.get(stem, 0), "product": in_products.get(stem, 0),
                       "source": c["fm"].get("source", "—")})

    scored.sort(key=lambda x: (-x["score"], x["path"]))
    total = len(cards)
    verified = sum(by_status.get(s, 0) for s in TRUSTED)   # canonical — легаси
    pct = (verified / total * 100) if total else 0.0

    out = [f"# Очередь верификации — {TODAY}", "",
           f"Карточек: {total} · verified: {verified} ({pct:.1f} %) · "
           f"кандидатов с ненулевой ценностью: {len(scored)}", ""]
    if not usage:
        out += ["> `meta/usage.log` пуст: вес по употреблению в context pack не учитывается.",
                "> Политика ретрива должна дописывать в него строку на каждую карточку в паке.", ""]
    out += [f"## Топ-{min(a.limit, len(scored))} к верификации", "",
            "| # | Карточка | Раздел | Статус | Вес | pack | ссылки | продукты | Источник |",
            "|---|---|---|---|---|---|---|---|---|"]
    stem_count = Counter(c["stem"] for c in cards.values())
    for i, s in enumerate(scored[:a.limit], 1):
        twin = " ⚠️двойник" if stem_count[s["stem"]] > 1 else ""
        out.append(f"| {i} | [[{s['stem']}]]{twin} | {s['section']} | {s['status']} | {s['score']} | "
                   f"{s['usage']} | {s['inbound']} | {s['product']} | {s['source'][:60]} |")
    if any(stem_count[s["stem"]] > 1 for s in scored[:a.limit]):
        out += ["", "> ⚠️двойник — карточка с таким именем есть в нескольких разделах. "
                "Сначала слейте (`kb_fix.py --dupes`), потом верифицируйте — иначе проверите не ту."]

    groups = defaultdict(int)
    for s in scored[:a.limit]:
        groups[s["section"]] += 1
    if groups:
        out += ["", "## Пакетами (одна тема = один заход `verify`)", ""]
        for sec, n in sorted(groups.items(), key=lambda x: -x[1]):
            out.append(f"- `{sec}` — {n} карточек: `/aurora-vault verify {sec}` "
                       f"(быстрый режим: «прими с дефолтами»)")

    if expired:
        out += ["", f"## Протухшие verified: {len(expired)} — подробности в `aurora_stats.py`",
                "", "Перепроверка — отдельная работа, не первичная верификация."]

    out += ["", "## Статусы базы", ""] + [f"- {k}: {v}" for k, v in by_status.most_common()]

    report = "\n".join(out)
    print(report)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\nОтчёт: {a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
