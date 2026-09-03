#!/usr/bin/env python3
"""kb_translit.py — словарь имён: латиница ↔ кириллица, один перевод на понятие.

Панель: `kb:translit`

Источники приходят с разными именами страниц: часть названа по-русски, часть
транслитом — «SPR-001-Statusy-tarifa» вместо «SPR-001 Статусы тарифа». Карточка наследует
имя источника, и дальше происходит расщепление: в тексте других карточек то же понятие
названо кириллицей, ссылка `[[Статусы тарифа]]` до транслитерованной карточки не доходит,
а `kb:repair --stubs` заводит под неё пустышку. Одно понятие, две карточки, ни одной
связи между ними.

Механически транслит развернуть нельзя: «Statusy» → «Статусы» угадывается, а
«Tipy-stavok-aktsiza» — уже нет, и обратная таблица даёт «Типы ставок акциза» лишь
случайно. Поэтому перевод делается **один раз** и записывается сюда; дальше он берётся
из словаря, а не придумывается заново. Второй разбор того же понятия обязан получить то
же имя — иначе база расщепится ровно так же, только позже.

Словарь — `AuroraKnowledgeDB/meta/translit.md`, обычная таблица, читаемая человеком:

    | Латиницей | Кириллицей | Добавлено | Кем |
    |---|---|---|---|
    | SPR-001-Statusy-tarifa | SPR-001 Статусы тарифа | 2026-09-03 | kb:translit |

  python3 .opencode/scripts/kb_translit.py              # что найдено и что предложено
  python3 .opencode/scripts/kb_translit.py --apply      # дописать находки в словарь
  python3 .opencode/scripts/kb_translit.py --rename --apply   # переименовать по словарю

Зависимостей нет.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aurora_common import (KB_ROOT, card_filename, frontmatter,  # noqa: E402
                           git_guard, is_service, link_refs, load_cards,
                           rewrite_links, walk_md)

DICT_PATH = os.path.join(KB_ROOT, "meta", "translit.md")
TODAY = date.today().isoformat()

# Строка словаря: латиница | кириллица | дата | кем.
ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", re.M)

HEAD = """# Словарь имён: латиница → кириллица

Один перевод на понятие, записанный при первой встрече и переиспользуемый дальше.

Зачем. Источники приходят с разными именами страниц: часть по-русски, часть транслитом.
Карточка наследует имя источника, и понятие расщепляется — в других карточках оно названо
кириллицей, ссылка до транслитерованной карточки не доходит, и под неё заводится пустышка.
Одно понятие, две карточки, ни одной связи.

Механически транслит не развернуть: «Statusy» ещё угадывается, «Tipy-stavok-aktsiza» —
уже нет. Поэтому перевод делается один раз, руками или моделью, и живёт здесь. Правьте
правую колонку — движок читает её, а не придумывает заново.

Строку добавляет `kb:translit`; перевод в ней может быть пустым — это значит «переведите».
Переименование по словарю — `kb:translit --rename --apply`.

| Латиницей | Кириллицей | Добавлено | Кем |
|---|---|---|---|
"""


def has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", s))


def is_latin_name(stem: str) -> bool:
    """Имя набрано латиницей, хотя понятие русское: ни одной кириллической буквы.

    Коды и идентификаторы («ALG-105», «SPR-001») сами по себе латиницей нормальны —
    имя целиком из кода и цифр не считаем транслитом. Транслит — это когда латиницей
    записаны слова.
    """
    if has_cyrillic(stem):
        return False
    words = [w for w in re.split(r"[-_. ]+", stem) if w]
    # хотя бы одно слово из букв длиной от четырёх: «Statusy», «Tipy» — да; «SPR», «001» — нет
    return any(len(w) >= 4 and w.isalpha() and not w.isupper() for w in words)


def read_dict(path: str = DICT_PATH) -> dict:
    """{латиницей: кириллицей}. Пустой перевод — строка есть, перевода ещё нет."""
    out = {}
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return out
    for lat, cyr in ROW.findall(text):
        lat, cyr = lat.strip(), cyr.strip()
        if not lat or lat.startswith("-") or lat.lower() == "латиницей":
            continue
        out[lat] = cyr if has_cyrillic(cyr) else ""
    return out


def write_dict(rows: dict, path: str = DICT_PATH) -> None:
    """Перезаписать словарь целиком, сохранив уже сделанные переводы."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [HEAD]
    for lat in sorted(rows):
        cyr = rows[lat] or ""
        lines.append(f"| {lat} | {cyr} | {TODAY} | kb:translit |\n")
    open(path, "w", encoding="utf-8").write("".join(lines))


def latin_cards(root: str = KB_ROOT) -> list:
    """[(путь, имя)] — карточки, названные латиницей при русском содержимом."""
    out = []
    for path in walk_md(root, skip_service=True, skip_archive=True):
        stem = os.path.basename(path)[:-3]
        if stem.startswith("_") or not is_latin_name(stem):
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        # Латинское имя при латинском содержимом — не транслит, а честное английское имя.
        if has_cyrillic(text):
            out.append((path.replace("\\", "/"), stem))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Словарь имён: латиница ↔ кириллица")
    ap.add_argument("--apply", action="store_true", help="дописать находки в словарь")
    ap.add_argument("--rename", action="store_true",
                    help="переименовать карточки по готовым переводам и переписать ссылки")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="работать по незакоммиченному дереву")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"kb_translit: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    known = read_dict()
    found = latin_cards()
    fresh = [(p, s) for p, s in found if s not in known]

    print(f"# Словарь имён — {TODAY}\n")
    print(f"Карточек с латинским именем при русском содержимом: **{len(found)}**")
    print(f"В словаре записей: **{len(known)}**, из них переведено: "
          f"**{sum(1 for v in known.values() if v)}**\n")

    if fresh:
        print(f"## Новые имена: {len(fresh)}\n")
        print("Перевод не угадывается механически — впишите его в правую колонку словаря "
              "руками\nили попросите модель. Записанный один раз, дальше он "
              "переиспользуется.\n")
        print("Не всякое латинское имя — транслит: `ER.TAC.SystemId` это идентификатор, а\n"
              "`Epic 3` — английское слово. У таких оставьте перевод пустым: "
              "переименование\nидёт только по заполненным строкам.\n")
        for path, stem in fresh[:30]:
            print(f"- `{stem}` — {path}")
        if len(fresh) > 30:
            print(f"- … ещё {len(fresh) - 30}")
        print()

    ready = {s: known[s] for _p, s in found if known.get(s)}
    if ready:
        print(f"## Готовы к переименованию: {len(ready)}\n")
        for lat, cyr in sorted(ready.items())[:20]:
            print(f"- `{lat}` → `{cyr}`")
        print()

    if a.rename:
        if not ready:
            print("Переводов в словаре нет — переименовывать нечего.")
            return 0
        if a.apply and not git_guard(KB_ROOT, a.allow_dirty, "переименование по словарю"):
            return 2
        cards = load_cards()
        renamed = 0
        for path, stem in found:
            cyr = known.get(stem)
            if not cyr:
                continue
            new_name = card_filename(cyr)
            new_path = os.path.join(os.path.dirname(path), new_name + ".md")
            if os.path.exists(new_path):
                print(f"  ⚠️  {new_path} уже занят — {stem} пропущен")
                continue
            print(f"{'✅' if a.apply else '(dry-run)'} {stem} → {new_name}")
            if not a.apply:
                renamed += 1
                continue
            text = open(path, encoding="utf-8").read()
            # Старое имя уходит в синонимы: ссылки, набранные транслитом, обязаны
            # продолжать работать — их писали люди и они разбросаны по базе.
            text = add_alias(text, stem)
            open(path, "w", encoding="utf-8").write(text)
            os.rename(path, new_path)
            renamed += 1
        if a.apply:
            for path in walk_md(KB_ROOT, skip_service=False, skip_archive=True):
                text = open(path, encoding="utf-8", errors="ignore").read()
                fixed = rewrite_links(text, {lat: known[lat] for lat in ready})
                if fixed != text:
                    open(path, "w", encoding="utf-8").write(fixed)
        print(f"\n{'✅ Переименовано' if a.apply else '(dry-run) К переименованию'}: {renamed}")
        return 0

    if a.apply:
        rows = dict(known)
        for _p, stem in found:
            rows.setdefault(stem, "")
        write_dict(rows)
        print(f"✅ Словарь обновлён: {DICT_PATH} (записей {len(rows)})")
        print("Впишите переводы в правую колонку, затем `kb:translit --rename --apply`.")
    else:
        print("(dry-run) Словарь не тронут. Дописать находки: `--apply`")
    return 0


def add_alias(text: str, name: str) -> str:
    """Дописать синоним в шапку, не трогая остального."""
    if re.search(r"^\s*-\s*\"?" + re.escape(name) + r"\"?\s*$", text, re.M):
        return text
    m = re.search(r"^aliases:\s*(\[\]|\[.*\])?\s*$", text, re.M)
    if not m:
        return re.sub(r"^(---\n)", f'\\1aliases:\n  - "{name}"\n', text, count=1)
    if (m.group(1) or "[]").strip() in ("", "[]"):
        return text[:m.start()] + f'aliases:\n  - "{name}"' + text[m.end():]
    inner = m.group(1).strip()[1:-1].strip()
    return text[:m.start()] + "aliases:\n" + "".join(
        f'  - {x.strip()}\n' for x in inner.split(",") if x.strip()
    ) + f'  - "{name}"' + text[m.end():]


if __name__ == "__main__":
    sys.exit(main())
