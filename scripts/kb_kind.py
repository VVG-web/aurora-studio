#!/usr/bin/env python3
"""kb_kind.py — тип карточки: словарь, документ или знание (фреймворк «Аврора»).

Тип решает, что движку **можно делать с телом**, и цена ошибки здесь наибольшая в базе:
неверный тип означает либо потерю дословного текста, либо мёртвую карточку, которую
никогда не переосмыслят.

  python3 .aurora/scripts/kb_kind.py            # что будет проставлено
  python3 .aurora/scripts/kb_kind.py --apply

Три типа и их правила:

    dictionary  словари, справочники, перечисления. Переносятся целиком, одна карточка на
                справочник. Модель даёт имя и `summary`, тело не трогает.
    document    источник — нормативный текст: договор, ТЗ, регламент, печатная форма.
                Текст перенесён дословно и менять его запрещено: он и есть ценность.
    knowledge   всё остальное. Модель пишет тезис своими словами, цитаты источника
                уходят в подвал. Переосмысляется при каждом обогащении.

Тип определяется правилом, а не вкусом модели: папка источника, раздел базы, характер
текста. И пересчитывается на каждом прогоне: карточка копит источники, и документ из одной
бумаги, вобравший ещё четыре страницы, — уже знание.

**Слово человека — исправление.** До 1.146.0 любой записанный `kind` считался «выбором
человека» и не пересчитывался, хотя пишет его сам движок: на PRJ-C тип расходился с
правилом у 92 карточек, PRJ-A — у 38, PRJ-B — у 21. Человек задаёт тип так же, как любое
своё слово: полем `kind:` в шапке исправления (`Raw/corrections/`, `corrects: "[[Карточка]]"`).

Панель: `kb:kind`
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (card_sources, frontmatter, is_cited_unit, is_record_mirror, link_escapes, name_lineage,  # noqa: E402
                           split_frontmatter, walk_md, with_fields)
from card_types import run_script, fit_kind, registry_for, try_write_card  # noqa: E402 — вид в пределах видов типа; вход записи

from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок
KB = "AuroraKnowledgeDB"
KINDS = ("dictionary", "document", "knowledge")

# Разделы, где лежит именование, а не выводы: термины, справочники, перечисления.
DICT_SECTIONS = {"Glossary", "Reference", "Statuses"}
# Папки первоисточников: нормативный текст, который переносится дословно. Решения и вопросы — записи человека (`kb:record`):
# их зеркало — дословный документ. `Raw/reference` сюда не входит: зеркало справочника — словарь (раздел `Reference`).
DOC_ROOTS = ("Raw/laws", "Raw/contract", "Raw/customer", "Raw/project", "Raw/dictionaries", "Raw/decisions", "Raw/questions")
# Слова в имени источника, по которым видно нормативный документ. «Контракт» — только в
# правовом смысле: в ИТ-проекте это чаще интеграционный контракт, и «Маппинг контракта
# ВАЛЮТА» становился документом, которому тезис не пишут никогда (PRJ-C). «Печатная форма»
# там же — спецификация экрана, а не бумага.
DOC_WORDS = re.compile(r"(?i)(договор|государственн\w*[ _-]+контракт|госконтракт|"
                       r"контракт\w*[ _-]*(№|n[o°]?[ _-]*\d)|техническое[ _-]задание|\bТЗ\b|"
                       r"регламент|приложение[ _-]№|форма[ _-]отч|устав|положение|приказ)")
# Таблица шагов алгоритма и таблица требований экранной формы — знание, а не справочник.
TABLE_IS_KNOWLEDGE = {"Processes", "Requirements"}
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
    if section not in TABLE_IS_KNOWLEDGE and looks_like_table(body):
        return "dictionary", "тело — таблица кодов без прозы"
    return "knowledge", "обычное знание: тезис пишется и переосмысляется"


def human_kinds(root: str = ".") -> dict:
    """{имя карточки: тип} — из шапок действующих исправлений человека."""
    out: dict = {}
    folder = os.path.join(root, "Raw", "corrections")
    if not os.path.isdir(folder):
        return out
    for f in sorted(os.listdir(folder)):
        if not f.endswith(".md") or f.startswith("_") or link_escapes(os.path.join(folder, f)):
            continue
        try:
            fm = frontmatter(open(os.path.join(folder, f), encoding="utf-8",
                                  errors="ignore").read()) or {}
        except OSError:
            continue
        kind = (fm.get("kind") or "").strip().strip('"')
        if kind not in KINDS or (fm.get("status") or "").strip().strip('"') == "archived":
            continue
        for name in re.findall(r"\[\[([^\]|#]+)", fm.get("corrects") or ""):
            out[name.strip()] = kind
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Тип карточки: словарь, документ или знание")
    ap.add_argument("--apply", action="store_true", help="записать kind в карточки")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    root = a.root
    if not os.path.isdir(os.path.join(root, KB)):
        print("kb_kind: нет AuroraKnowledgeDB/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    counts, set_now, kept, refused = {}, [], 0, []
    reg = registry_for(root)
    paths = list(walk_md(os.path.join(root, KB), skip_service=True, skip_archive=True))
    human = human_kinds(root)
    # Исправление называет карточку по имени, а имя меняется (синоним после снятия кода
    # документа, слияние и замена через `superseded_by`): тип остаётся за той же карточкой.
    live: dict = {}
    for path in paths:
        live.setdefault(os.path.splitext(os.path.basename(path))[0], []).append(path)
    missing = [n for n in human if n not in live]
    if missing:
        was_names = name_lineage(os.path.join(root, KB), live, missing)
        for n, k in list(human.items()):
            to = was_names.get(n) if n not in live else None
            if to:
                human.setdefault(os.path.splitext(os.path.basename(to[0]))[0], k)
    for path in paths:
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
        if was == "template":
            continue            # форма проекта (`kb:repair --unparsed`): тип ей дан при заведении
        if is_record_mirror(fm, text):
            continue            # зеркало записи: вид задан типом записи и мастером (`kb:record --mirror`), правило его не трогает
        stem = os.path.splitext(os.path.basename(path))[0]
        if is_cited_unit(fm):
            # единица закона или контракта (`kb:cite`): текст дословный всегда, и слово человека этого не меняет
            kind, why = "document", "единица цитируемого документа: дословный текст"
        elif human.get(stem):
            kind, why = human[stem], "так сказал человек (Raw/corrections)"
            kept += 1
        else:
            kind, why = guess(os.path.relpath(path, root), fm, body or "",
                              card_sources(text))
            # Правило не знает видов типа: роль не бывает «документом». Вид — в пределах шаблона типа, но только
            # там, где он меняется: уже стоящий вид (пусть и не из списка типа) карточке не пересчитывается.
            fitted = fit_kind((fm.get("type") or "").strip(), kind, reg) if kind != was else kind
            if fitted != kind:
                why, kind = f"{why}; у типа «{fm.get('type')}» такого вида нет — {fitted}", fitted
        counts[kind] = counts.get(kind, 0) + 1
        if kind == was:
            continue
        set_now.append((os.path.relpath(path, root), kind,
                        (f"было {was} — " if was else "") + why))
        if a.apply:
            _done, why_not = try_write_card(path, with_fields(text, {"kind": kind}), op="header", old_text=text, reg=reg)
            if why_not:
                refused.append((os.path.relpath(path, root), why_not))

    print(f"# Тип карточек — {TODAY}\n")
    print("| Тип | Карточек | Что можно делать с телом |")
    print("|---|---|---|")
    for k, what in (("dictionary", "переносится целиком, модель не трогает"),
                    ("document", "дословно, менять запрещено"),
                    ("knowledge", "тезис пишется и переосмысляется")):
        print(f"| {k} | {counts.get(k, 0)} | {what} |")
    print(f"\nПроставить или пересчитать: {len(set_now)} · по исправлению человека: {kept}")
    for rel, kind, why in set_now[:8]:
        print(f"  - {rel}: {kind} — {why}")
    if len(set_now) > 8:
        print(f"  … ещё {len(set_now) - 8}")
    if refused:
        print(f"\nНе записано шаблоном типа: {len(refused)}")
        for rel, why_not in refused[:8]:
            print(f"  ! {rel}: {why_not}")
    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(run_script(main))
