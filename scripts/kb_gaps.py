#!/usr/bin/env python3
"""kb_gaps.py — смысловые дыры базы: чего в ней не хватает и что рассыпается.

Панель: `ops:gaps`

`kb:lint` проверяет механику: битые ссылки, схему, статусы. Он не видит того, от чего
база перестаёт быть базой, — а именно это и убивает картотеки:

  ПОНЯТИЕ БЕЗ КАРТОЧКИ   сущность названа в пяти карточках, своей у неё нет. Знание о
                         ней размазано по чужим телам и не находится по имени;
  СВЯЗЬ НЕ ПОСТАВЛЕНА    карточка называет сущность, у которой карточка ЕСТЬ, и не
                         ссылается на неё. Связь — часть мысли: без неё граф врёт, а
                         человек не доходит до знания, которое рядом;
  ОДИНОКАЯ КАРТОЧКА      ни входящих, ни исходящих. В зеттелькастене такой не бывает:
                         карточка, ни с чем не связанная, не участвует в мышлении;
  ТЕЗИС ОТСТАЛ           источник переписан позже, чем собран тезис. Карточка отвечает
                         по прежнему тексту и не знает об этом;
  ОБРЫВ ПРОИСХОЖДЕНИЯ    источник карточки исчез с диска: сверить знание не с чем.

Ничего не правит. Это отчёт: что чинится командой — сказано прямо, что решает человек —
названо человеком.

  python3 .opencode/scripts/kb_gaps.py                 # отчёт
  python3 .opencode/scripts/kb_gaps.py --min-mentions 3  # порог для «понятия без карточки»
  python3 .opencode/scripts/kb_gaps.py --report meta/gaps.md

Зависимостей нет.
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aurora_common import (KB_ROOT, QUOTES, aliases, card_body,  # noqa: E402
                           card_sources,
                           frontmatter, is_placeholder, leaf_name, link_refs,
                           related_targets, walk_md)

TODAY = date.today().isoformat()

# Кандидат в сущности — аббревиатура или код: заглавные буквы, цифры, дефис.
TERM_RE = re.compile(r"\b([А-ЯЁA-Z][А-ЯЁA-Z0-9]{1,}(?:[-_.][А-ЯЁA-Z0-9]+)*)\b")

# Длинное слово капсом — это выделение, а не сокращение: «ОБЯЗАТЕЛЬНО», «ВНИМАНИЕ».
# Настоящие аббревиатуры коротки; порог по длине отсекает целый класс шума одним числом,
# не требуя вести список слов, который на каждом проекте будет свой.
ABBR_MAX = 8

# Формат, разметка, инструмент — не знание предметной области. Список общий для всех
# проектов: доменные сокращения сюда не попадают, а эти встречаются в любой базе.
STOP = {"ЕСЛИ", "ТОЛЬКО", "ВАЖНО", "НЕТ", "ДА", "ПУСТО", "TODO", "FIXME", "JSON", "XML",
        "HTTP", "HTTPS", "URL", "API", "ID", "PDF", "CSV", "SQL", "UTF", "MD", "YAML",
        "HTML", "PNG", "JPG", "SVG", "GIT", "JIRA", "BPMN", "UML", "ERD", "UUID", "GUID",
        "CRUD", "REST", "SOAP", "GUI", "UI", "UX", "OK", "NULL", "TRUE", "FALSE",
        "SAVE", "DELETE", "UPDATE", "INSERT", "SELECT", "GET", "POST", "PUT"}


def thesis_of(text: str) -> str:
    """Своя часть карточки — то, что написала модель, до дословного текста источника."""
    return card_body(text).split(QUOTES, 1)[0]


def load(root: str = KB_ROOT) -> dict:
    """{имя: карточка}. Карты содержания и оглавления — не карточки: они навигация."""
    out = {}
    for path in walk_md(root, skip_service=True, skip_archive=True):
        stem = os.path.basename(path)[:-3]
        if stem.startswith("_"):
            continue
        section = os.path.relpath(path, root).replace("\\", "/").split("/")[0]
        if section == "MOC":
            continue
        text = open(path, encoding="utf-8", errors="ignore").read()
        fm = frontmatter(text)
        body = thesis_of(text)
        out[stem] = {
            "path": path, "section": section, "fm": fm, "text": text, "thesis": body,
            "ph": is_placeholder(fm, text),
            # Длину здесь не режем. Порог в четыре буквы отбрасывал имена карточек
            # вроде «ИНН» и «КПП» — и они числились понятиями без карточки, хотя
            # карточка есть: на живой базе три из двадцати двух «наверняка сущностей»
            # были такой выдумкой отчёта. Где порог нужен, он стоит по месту.
            "names": {n for n in ({stem, (fm.get("title") or "").strip().strip('"')}
                                  | set(aliases(text))) if n},
            "out": ({l.split("#")[0].strip() for l in link_refs(text) if "/" not in l}
                    | {leaf_name(x) for x in related_targets(text)}),
        }
    return out


def missing_cards(cards: dict, floor: int) -> list:
    """[(термин, в скольких карточках назван)] — понятия без своей карточки.

    Порог по числу карточек, а не по числу упоминаний: термин, названный десять раз в
    одной карточке, — это её собственная тема, а не пробел в базе.
    """
    known = set()
    for c in cards.values():
        known |= {n.lower() for n in c["names"]}
    seen = collections.Counter()
    for stem, c in cards.items():
        for term in {m.group(1) for m in TERM_RE.finditer(c["thesis"])}:
            if term in STOP or term.lower() in known or len(term) < 3:
                continue
            if term.isalpha() and len(term) > ABBR_MAX:
                continue          # длинное слово капсом — выделение, а не сокращение
            seen[term] += 1
    return [(t, n) for t, n in seen.most_common() if n >= floor]


def missing_links(cards: dict) -> list:
    """[(карточка, кого назвала и не связала)] — связь, которую не поставили."""
    out = []
    for stem, c in cards.items():
        if c["ph"] or not c["thesis"].strip():
            continue
        low = c["thesis"].lower()
        hit = []
        for other, oc in cards.items():
            if other == stem or other in c["out"] or oc["ph"]:
                continue
            if any(len(n) >= 5 and n.lower() in low for n in oc["names"]):
                hit.append(other)
        if hit:
            out.append((stem, sorted(hit)))
    return sorted(out, key=lambda r: -len(r[1]))


def behind_neighbour(cards: dict) -> list:
    """[(карточка, сосед, когда её тезис, когда правлен сосед)] — сосед ушёл вперёд.

    Отметки в базе привязаны к самой карточке: `relinked` и `extracted` держат дату её
    тезиса, «тезис отстал» сравнивает карточку с её файлом-источником. Соседей не
    проверял никто — поправили главную карточку, а пять ссылающихся на неё продолжают
    говорить прежнее. Здесь это видно числом и без обращений к модели; разбирает пары
    `agent:clashes`, он умеет цитировать обе стороны.
    """
    out = []
    for stem, c in sorted(cards.items()):
        if c["ph"]:
            continue
        mine = (c["fm"].get("distilled") or "").strip().strip('"')
        if not mine:
            continue
        for other in sorted(c["out"]):
            oc = cards.get(other)
            if not oc or other == stem or oc["ph"]:
                continue
            theirs = (oc["fm"].get("updated") or "").strip().strip('"')
            if theirs and theirs > mine:
                out.append((stem, other, mine, theirs))
    return out


def lonely(cards: dict) -> list:
    """Карточки без входящих и без исходящих — не участвующие в мышлении."""
    inbound = collections.Counter()
    for stem, c in cards.items():
        for tgt in c["out"]:
            if tgt in cards and tgt != stem:
                inbound[tgt] += 1
    return sorted(s for s, c in cards.items()
                  if not c["ph"] and inbound[s] == 0 and not (c["out"] & set(cards)))


def stale(cards: dict, root: str = KB_ROOT) -> tuple:
    """(тезис отстал от источника, источник исчез)."""
    late, gone = [], []
    for stem, c in cards.items():
        done = (c["fm"].get("distilled") or "").strip()
        for src in card_sources(c["text"]):
            if not src or src.startswith("http"):
                continue
            # Происхождение бывает записано словами: «legacy ZK cards (pre-Aurora)» — это
            # пометка человека о том, откуда знание, а не файл. Требовать её на диске
            # значит объявить пропавшими сорок карточек, у которых всё в порядке.
            if "/" not in src and not src.endswith(".md"):
                continue
            if not os.path.exists(src):
                gone.append((stem, src))
                continue
            if not done:
                continue
            try:
                touched = date.fromtimestamp(os.path.getmtime(src)).isoformat()
            except OSError:
                continue
            if touched > done:
                late.append((stem, src, done, touched))
    return late, gone


def main() -> int:
    ap = argparse.ArgumentParser(description="Смысловые дыры базы знаний")
    ap.add_argument("--min-mentions", type=int, default=3, metavar="N",
                    help="сколько карточек должны назвать понятие, чтобы считать его "
                         "пробелом (по умолчанию 3)")
    ap.add_argument("--limit", type=int, default=25, help="сколько строк печатать в разделе")
    ap.add_argument("--report", metavar="ФАЙЛ", default="", help="записать отчёт в файл")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"kb_gaps: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    cards = load()
    if not cards:
        print("Карточек нет — сначала `kb:build`.")
        return 0

    terms = missing_cards(cards, a.min_mentions)
    from aurora_common import project_terms
    glossary = project_terms()
    sure = [(t, n) for t, n in terms if t in glossary]
    maybe = [(t, n) for t, n in terms if t not in glossary]
    links = missing_links(cards)
    alone = lonely(cards)
    late, gone = stale(cards)
    behind = behind_neighbour(cards)
    unlinked_pairs = sum(len(v) for _s, v in links)

    L = [f"# Смысловые дыры базы — {TODAY}", "",
         f"Карточек: **{len(cards)}** · понятий без карточки: **{len(terms)}** · "
         f"непоставленных связей: **{unlinked_pairs}** · одиноких: **{len(alone)}** · "
         f"тезисов отстало: **{len(late)}** · отстало от соседа: **{len(behind)}** · "
         f"оборванных источников: **{len(gone)}**", ""]

    L += ["## Сосед изменился, карточка — нет", ""]
    if not behind:
        L.append("Таких нет.")
    else:
        L += ["Карточка ссылается на другую, а ту правили позже: знание о соседе в ней "
              "могло устареть. Отметки движка привязаны к самой карточке, соседей до "
              "1.100.39 не проверял никто.", "",
              "Разбирает `agent:clashes`: он берёт эти пары первыми и цитирует обе "
              "стороны — решать, кто прав, человеку.", "",
              "| Карточка | Сосед | Тезис от | Сосед правлен |", "|---|---|---|---|"]
        L += [f"| `{a}` | `{b}` | {m} | {t} |" for a, b, m, t in behind[:40]]
        if len(behind) > 40:
            L.append(f"\n… ещё {len(behind) - 40}.")
        L.append("")

    L += ["## Понятие названо, карточки нет", ""]
    if not terms:
        L.append("Таких нет.")
    else:
        L += [f"Сущность названа в нескольких карточках, своей у неё нет: знание о ней "
              f"размазано по чужим телам и не находится по имени. Порог — "
              f"{a.min_mentions} карточки.", "",
              "Завести карточку — работа разбора (`agent:extract` выносит определение, "
              "если оно есть в тексте) или человека, если знания о сущности в базе пока "
              "нет вовсе.", ""]
        if sure:
            L += [f"### Наверняка сущности: {len(sure)}", "",
                  "У них есть расшифровка в словаре проекта — значит, это понятия "
                  "предметной области, а не разметка и не формат.", "",
                  "| Понятие | В скольких карточках | Что это |", "|---|---|---|"]
            L += [f"| `{t}` | {n} | {glossary[t][:70]} |" for t, n in sure[:a.limit]]
            if len(sure) > a.limit:
                L.append(f"| … ещё {len(sure) - a.limit} | | |")
            L.append("")
        if maybe:
            L += [f"### Похоже на сущности: {len(maybe)}", "",
                  "Расшифровки в словаре нет — может быть и понятием, и кодом поля, и "
                  "чужим именем. Смотрит человек.", "",
                  "| Понятие | В скольких карточках |", "|---|---|"]
            L += [f"| `{t}` | {n} |" for t, n in maybe[:a.limit]]
            if len(maybe) > a.limit:
                L.append(f"| … ещё {len(maybe) - a.limit} | |")
    L.append("")

    L += ["## Связь названа, но не поставлена", ""]
    if not links:
        L.append("Таких нет.")
    else:
        L += ["Карточка называет сущность, у которой карточка ЕСТЬ, и не ссылается на "
              "неё. Связь — часть мысли, а не украшение: без неё граф показывает "
              "связность, которой нет, и человек не доходит до знания, лежащего рядом.",
              "", "Ставит связи тот, кто пишет тезис (`agent:distill`). Массово — "
              "перезапуск тезисов по этим карточкам.", "",
              "| Карточка | Кого назвала и не связала |", "|---|---|"]
        for stem, hits in links[:a.limit]:
            L.append(f"| `{stem}` | " + ", ".join(f"[[{h}]]" for h in hits[:6])
                     + (" …" if len(hits) > 6 else "") + " |")
        if len(links) > a.limit:
            L.append(f"| … ещё {len(links) - a.limit} | |")
    L.append("")

    L += ["## Одинокие карточки", ""]
    if not alone:
        L.append("Таких нет.")
    else:
        L += ["Ни входящих связей, ни исходящих. В картотеке такой карточки не бывает: "
              "знание, ни с чем не связанное, не участвует в мышлении и не находится "
              "иначе как перебором.", ""]
        L += [f"- `{s}`" for s in alone[:a.limit]]
        if len(alone) > a.limit:
            L.append(f"- … ещё {len(alone) - a.limit}")
    L.append("")

    if late:
        L += ["## Тезис отстал от источника", "",
              "Источник переписан позже, чем собран тезис: карточка отвечает по прежнему "
              "тексту и не знает об этом. Лечится `agent:distill` — он снимает отметку и "
              "пересобирает.", "", "| Карточка | Источник | Тезис | Источник правлен |",
              "|---|---|---|---|"]
        L += [f"| `{s}` | `{src}` | {d} | {t} |" for s, src, d, t in late[:a.limit]]
        L.append("")
    if gone:
        L += ["## Источник исчез", "",
              "Файла, из которого собрана карточка, на диске нет: сверить знание не с чем. "
              "Либо зеркало не досинхронизировано (`sync:audit`), либо страницу удалили в "
              "источнике — и тогда решать человеку, остаётся ли знание.", ""]
        L += [f"- `{s}` ← `{src}`" for s, src in gone[:a.limit]]
        L.append("")

    text = "\n".join(L).rstrip() + "\n"
    print(text)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        open(a.report, "w", encoding="utf-8").write(text)
        print(f"✅ Отчёт записан: {a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
