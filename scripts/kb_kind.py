#!/usr/bin/env python3
"""kb_kind.py — тип карточки: словарь, документ или знание (фреймворк «Аврора»).

Тип решает, что движку **можно делать с телом**, и цена ошибки здесь наибольшая в базе:
неверный тип означает либо потерю дословного текста, либо мёртвую карточку, которую
никогда не переосмыслят.

  python3 .opencode/scripts/kb_kind.py            # что будет проставлено
  python3 .opencode/scripts/kb_kind.py --apply

Три типа и их правила:

    dictionary  словари, справочники, перечисления. Переносятся целиком, одна карточка на
                справочник. Модель даёт имя и `summary`, тело не трогает.
    document    источник — нормативный текст: договор, ТЗ, регламент, печатная форма.
                Текст перенесён дословно и менять его запрещено: он и есть ценность.
    knowledge   всё остальное. Модель пишет тезис своими словами, цитаты источника
                уходят в подвал. Переосмысляется при каждом обогащении.

Тип определяется правилом, а не вкусом модели: папка источника, раздел базы, характер
текста. Спорное не угадывается — оно попадает в `ops:todo` списком.

**Выбор человека сильнее правила.** Если `kind` уже стоит в карточке, движок его не
перетирает: человек мог знать про документ то, чего не знает эвристика.

Панель: `kb:kind`
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (card_sources, frontmatter, split_frontmatter,  # noqa: E402
                           walk_md, with_fields)

TODAY = date.today().isoformat()
KB = "AuroraKnowledgeDB"
KINDS = ("dictionary", "document", "knowledge")

# Разделы, где лежит именование, а не выводы: термины, справочники, перечисления.
DICT_SECTIONS = {"Glossary", "Reference", "Statuses"}
# Папки первоисточников: нормативный текст, который переносится дословно.
DOC_ROOTS = ("Raw/contract", "Raw/customer", "Raw/project", "Raw/dictionaries")
# Слова в имени источника, по которым видно нормативный документ.
DOC_WORDS = re.compile(r"(?i)(договор|контракт|техническое[ _-]задание|\bТЗ\b|регламент|"
                       r"приложение[ _-]№|печатн|форма[ _-]отч|устав|положение|приказ)")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)


def looks_like_table(body: str) -> bool:
    """Справочник узнаётся по форме: таблица или список кодов, а не проза."""
    rows = TABLE_ROW.findall(body)
    prose = [l for l in body.splitlines()
             if l.strip() and not l.strip().startswith(("|", "-", "*", "#", ">", "_"))]
    return len(rows) >= 4 and len(prose) <= 3


def guess(path: str, fm: dict, body: str, sources: list | None = None) -> tuple:
    """(тип, почему) — правило, а не суждение модели.

    `sources` — откуда в карточке знание. Список, а не строка: карточка накапливает его
    из нескольких артефактов, и тип зависит от происхождения целиком.
    """
    section = os.path.relpath(path, KB).replace("\\", "/").split("/")[0]
    # Источников может быть несколько. Документом карточку делает происхождение целиком:
    # текст нормативной бумаги ценен дословно, а карточка, вобравшая ещё четыре артефакта,
    # уже не бумага — это знание о сущности, и переписывать его тезисом можно.
    srcs = list(sources or []) or [""]
    src = srcs[0]
    if len(srcs) == 1 and any(src.startswith(r) for r in DOC_ROOTS):
        return "document", f"источник в {src.split('/')[1] if '/' in src else 'Raw'} — нормативный текст"
    if len(srcs) == 1 and DOC_WORDS.search(os.path.basename(src)):
        return "document", "имя источника говорит о нормативном документе"
    if section in DICT_SECTIONS:
        return "dictionary", f"раздел {section} — именование, а не выводы"
    if looks_like_table(body):
        return "dictionary", "тело — таблица кодов без прозы"
    return "knowledge", "обычное знание: тезис пишется и переосмысляется"


def main() -> int:
    ap = argparse.ArgumentParser(description="Тип карточки: словарь, документ или знание")
    ap.add_argument("--apply", action="store_true", help="записать kind в карточки")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    root = a.root
    if not os.path.isdir(os.path.join(root, KB)):
        print("kb_kind: нет AuroraKnowledgeDB/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    counts, set_now, kept = {}, [], 0
    for path in walk_md(os.path.join(root, KB), skip_service=True, skip_archive=True):
        text = open(path, encoding="utf-8", errors="ignore").read()
        # `split_frontmatter` отдаёт шапку БЕЗ разделителей, а хвост — начиная с «\n---».
        # Собирать файл обратно надо ровно этой парой: любая попытка отрезать хвост по
        # длине шапки промахивается на три символа и вклеивает поле в чужую строку.
        head, rest = split_frontmatter(text)
        if head is None:
            continue
        body = rest
        fm = frontmatter(text)
        if (fm.get("status") or "").strip() == "index":
            continue
        was = (fm.get("kind") or "").strip().strip('"')
        if was in KINDS:
            kept += 1
            counts[was] = counts.get(was, 0) + 1
            continue
        kind, why = guess(os.path.relpath(path, root), fm, body or "",
                          card_sources(text))
        counts[kind] = counts.get(kind, 0) + 1
        set_now.append((os.path.relpath(path, root), kind, why))
        if a.apply:
            open(path, "w", encoding="utf-8").write(with_fields(text, {"kind": kind}))

    print(f"# Тип карточек — {TODAY}\n")
    print("| Тип | Карточек | Что можно делать с телом |")
    print("|---|---|---|")
    for k, what in (("dictionary", "переносится целиком, модель не трогает"),
                    ("document", "дословно, менять запрещено"),
                    ("knowledge", "тезис пишется и переосмысляется")):
        print(f"| {k} | {counts.get(k, 0)} | {what} |")
    print(f"\nПроставить: {len(set_now)} · выбор человека сохранён: {kept}")
    for rel, kind, why in set_now[:8]:
        print(f"  - {rel}: {kind} — {why}")
    if len(set_now) > 8:
        print(f"  … ещё {len(set_now) - 8}")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
