#!/usr/bin/env python3
"""kb_twins.py — карточки, несущие одно и то же знание. Отчёт человеку, не слияние.

Панель: `kb:twins`

`kb:dedupe` ищет двойников по **имени**: одно имя после свёртки регистра, общий синоним,
одинаковый title. Этого мало. На живом проекте одну и ту же таблицу кодов состояния
несли **десять** карточек с разными именами — справочник-источник, словарь раздела,
разбор делового назначения, процесс, где она применяется. Ни одна пара не совпадала по
имени, и ремонт их не видел.

Вред от таких двойников не косметический:

  ссылки       расходятся по копиям, и граф показывает связность, которой нет;
  правки       ложатся в одну копию, остальные продолжают говорить прежнее — база сама
               себе противоречит, и обе стороны выглядят одинаково достоверно;
  поиск        отдаёт случайную из копий, а замер качества считает это промахом;
  контекст     тратится на повтор: одно знание занимает место трёх.

Мера — доля общих кусков текста (шинглов по 8 слов). Совпадение по шинглам означает не
«о том же самом», а «теми же словами»: пересказ одного факта разными словами сюда не
попадёт, и это правильно — такие карточки сводит человек, а не скрипт.

Ничего не переписывает. Решение о слиянии принимает человек: `kb:dedupe --merge
«оставить» «убрать»`. Скрипт называет группу, показывает, чем карточки похожи, и
предлагает, кого оставить, — по объёму, статусу и входящим ссылкам.

  python3 .opencode/scripts/kb_twins.py                 # отчёт
  python3 .opencode/scripts/kb_twins.py --min 0.5       # мягче порог (по умолчанию 0.6)
  python3 .opencode/scripts/kb_twins.py --report meta/twins.md

Зависимостей нет.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aurora_common import (KB_ROOT, card_body, inbound_counts,  # noqa: E402
                           is_placeholder, load_cards)

TODAY = date.today().isoformat()
SHINGLE = 8          # длина куска в словах
SKETCH = 96          # сколько наименьших хешей держим от карточки
MIN_WORDS = 60       # короче — сравнивать нечего: совпадут случайно
STATUS_RANK = {"knowledge": 3, "draft": 2, "placeholder": 0, "deprecated": 0, "": 1}


def shingles(text: str) -> set:
    """Куски по восемь слов, приведённые к сравнимому виду.

    Вёрстка, регистр и знаки выброшены: две копии одной таблицы отличаются выравниванием
    столбцов и жирностью, а знание в них одно.
    """
    words = re.findall(r"[\w\-]+", text.lower(), re.UNICODE)
    if len(words) < MIN_WORDS:
        return set()
    return {hashlib.blake2b(" ".join(words[i:i + SHINGLE]).encode(), digest_size=8).digest()
            for i in range(len(words) - SHINGLE + 1)}


def sketch(sh: set) -> set:
    """Наименьшие хеши — подпись карточки. Полные множества по базе не сравнить.

    Пар в базе на четыре тысячи карточек — восемь миллионов; сравнивать каждую с каждой
    целиком нельзя. Подпись из наименьших хешей даёт ту же долю совпадения с точностью
    в проценты, и по ней же строится обратный индекс, отсекающий заведомо чужих.
    """
    return set(sorted(sh)[:SKETCH])


def similarity(a: set, b: set) -> float:
    """Доля общего среди меньшей из подписей.

    Не Жаккар: справочник на сорок тысяч знаков и выписка из него на две тысячи —
    двойники, хотя пересечение к объединению у них мало. Вопрос не «одинаковы ли они»,
    а «сказано ли в меньшей то же, что в большей».
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def clusters(pairs: list) -> list:
    """Пары → группы: связанные попарно карточки собираются в одну семью."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _s in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict = {}
    for a, b, _s in pairs:
        groups.setdefault(find(a), set()).update((a, b))
    return [sorted(g) for g in groups.values()]


def keeper(paths: list, cards: dict, inbound: dict) -> tuple:
    """Кого предложить оставить и почему. Решает человек — это подсказка, не приговор."""
    def rank(p):
        c = cards[p]
        return (STATUS_RANK.get(c.status, 1), inbound.get(c.stem, 0), len(c.text))
    best = max(paths, key=rank)
    c = cards[best]
    why = []
    if STATUS_RANK.get(c.status, 1) >= 3:
        why.append(f"статус {c.status}")
    if inbound.get(c.stem, 0):
        why.append(f"на неё ссылаются {inbound[c.stem]}")
    why.append(f"{len(c.text)} знаков")
    return best, ", ".join(why)


def main() -> int:
    ap = argparse.ArgumentParser(description="Карточки, несущие одно знание")
    ap.add_argument("--min", type=float, default=0.6,
                    help="порог совпадения, 0..1 (по умолчанию 0.6)")
    ap.add_argument("--limit", type=int, default=40,
                    help="сколько групп печатать; 0 — все (отчёт читает `agent:twins`, "
                         "и урезанная печать урезала бы ему работу)")
    ap.add_argument("--report", metavar="ФАЙЛ", default="",
                    help="записать отчёт в файл (например meta/twins.md)")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"kb_twins: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    cards = {p: c for p, c in load_cards().items()
             if not is_placeholder(c.fm, c.text) and c.status != "deprecated"}
    raw = {}
    for path, c in cards.items():
        s = shingles(card_body(c.text))
        if s:
            raw[path] = s

    # Кусок текста, встречающийся в десятках карточек, — не знание, а вёрстка: шапка
    # таблицы, шаблонная формулировка, дисклеймер. Без отсева карточки маппинга
    # склеивались в «группу» из семидесяти штук: общими у них были заголовки столбцов,
    # а не содержание. Считаем, в скольких карточках встретился каждый кусок, и частые
    # выбрасываем — сравнивать надо то, что карточку отличает.
    df: dict = {}
    for s in raw.values():
        for h in s:
            df[h] = df.get(h, 0) + 1
    common = max(8, int(len(raw) * 0.004))
    sk = {}
    for path, s in raw.items():
        own = {h for h in s if df[h] <= common}
        if len(own) >= 12:
            sk[path] = sketch(own)
    if not sk:
        print("Карточек, пригодных для сравнения, нет.")
        return 0

    # Обратный индекс по подписям: кандидаты — только те, кто делит хотя бы один хеш.
    # Без него сравнение было бы квадратичным по всей базе.
    index: dict = {}
    for path, s in sk.items():
        for h in s:
            index.setdefault(h, []).append(path)
    seen_pairs, pairs = set(), []
    for h, paths in index.items():
        if len(paths) > 60:
            continue          # общий шаблон, а не знание: шапка, дисклеймер, шаблон карточки
        for i, p1 in enumerate(paths):
            for p2 in paths[i + 1:]:
                key = (p1, p2) if p1 < p2 else (p2, p1)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                s = similarity(sk[p1], sk[p2])
                if s >= a.min:
                    pairs.append((key[0], key[1], s))

    groups = sorted(clusters(pairs), key=len, reverse=True)
    inbound = inbound_counts(KB_ROOT)
    total = sum(len(g) for g in groups)

    out = [f"# Карточки, несущие одно знание — {TODAY}", "",
           f"Сравнивались {len(sk)} карточек из {len(cards)}. Порог совпадения — "
           f"{a.min:g} (доля общих кусков по восемь слов). Куски, встречающиеся более чем "
           f"в {common} карточках, отброшены как вёрстка.", "",
           f"**Групп: {len(groups)} · карточек в них: {total}.**", ""]
    if not groups:
        out.append("Двойников по содержимому не найдено.")
    else:
        out += ["Ничего не переписано: слияние — решение человека. Свести пару: "
                "`kb:dedupe --merge «оставить» «убрать»`.", "",
                "«Оставить» предложено по статусу, входящим ссылкам и объёму — "
                "проверьте, прежде чем сливать: длиннее не всегда значит полнее.", ""]
    # Ноль — «все», как и в остальных командах кита. Отчёт читает не только человек:
    # по нему работает `agent:twins`, и урезанная печать урезала бы ему работу молча —
    # прогон выглядел бы завершённым, обработав сорок групп из пятисот.
    shown = groups if not a.limit else groups[:a.limit]
    for g in shown:
        best, why = keeper(g, cards, inbound)
        out.append(f"## {len(g)} карточек · оставить `{cards[best].stem}` ({why})")
        out.append("")
        for p in g:
            c = cards[p]
            mark = "◀ оставить" if p == best else ""
            out.append(f"- `{c.stem}` · {c.status or '—'} · {len(c.text)} знаков · "
                       f"входящих {inbound.get(c.stem, 0)} {mark}")
        out.append("")
    if len(shown) < len(groups):
        out.append(f"… ещё групп: {len(groups) - len(shown)}")

    text = "\n".join(out)
    print(text)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        open(a.report, "w", encoding="utf-8").write(text + "\n")
        print(f"\n✅ Отчёт записан: {a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
