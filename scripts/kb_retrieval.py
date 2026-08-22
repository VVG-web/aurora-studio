#!/usr/bin/env python3
"""Отчёт выборки: какие карточки приходят первыми по эталонным запросам.

Панель: `ops:retrieval`

Ранжирование — то, на чём стоит всё остальное: ответ базы, обогащение перед
производством артефакта, инструменты ассистента. Менять его вслепую нельзя, а «стало
лучше» — не проверка. Отчёт печатает выдачу по запросам и **разницу с прошлым разом**:
«по запросу X первой стала другая карточка».

Запросы берутся из журналов разговоров (`meta/ask/`) — это настоящие вопросы аналитиков,
а не сочинённые под корпус. Их нет — работаем по списку из `meta/retrieval.txt`, по
строке на запрос.

Прошлый результат лежит рядом с журналами: сравнение глазами двух простыней по десять
запросов не работает, это проверено на себе.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

KB = "AuroraKnowledgeDB"
ASK = os.path.join(KB, "meta", "ask")
QUERIES = os.path.join(KB, "meta", "retrieval.txt")
STATE = os.path.join(KB, "meta", "retrieval-last.json")
TOP = 5


def queries(root: str, limit: int) -> list:
    """Запросы для проверки: свой список, иначе — вопросы из разговоров с базой."""
    path = os.path.join(root, QUERIES)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            own = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        if own:
            return own[:limit]
    out = []
    folder = os.path.join(root, ASK)
    for name in sorted(os.listdir(folder), reverse=True) if os.path.isdir(folder) else []:
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(folder, name), encoding="utf-8", errors="ignore").read()
        # Вопрос идёт следующей непустой строкой после заголовка «### Вопрос · дата» —
        # так его пишет `agent:ask`. Формат прочитан из живого журнала, а не угадан.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("### Вопрос"):
                continue
            q = next((x.strip() for x in lines[i + 1:i + 4] if x.strip()), "")
            if q and q not in out:
                out.append(q)
        if len(out) >= limit:
            break
    return out[:limit]


def rank(root: str, topic: str, semantic: bool) -> list:
    """[(имя карточки, вес)] — первые TOP по этому запросу.

    Считаем той же выборкой, что отвечает в «Спросить» и обогащает производство: своя
    копия ранжирования разошлась бы с настоящей, и отчёт стал бы сторожить не то.
    """
    import ctx_pack as P
    cwd = os.getcwd()
    os.chdir(root)
    try:
        cards = P.load_cards()
        P.measure_rarity(cards)
        close = P.semantic(topic, TOP * 4) if semantic else {}
        scored = sorted(((P.score(c, topic, close), c.stem) for c in cards.values()),
                        key=lambda x: (-x[0], x[1]))
    finally:
        os.chdir(cwd)
    return [(stem, s) for s, stem in scored[:TOP] if s > 0]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Выдача по эталонным запросам и разница с прошлым прогоном")
    ap.add_argument("--query", action="append", metavar="ТЕКСТ", default=[],
                    help="проверить этот запрос (можно повторять); иначе берутся "
                         "реальные вопросы из meta/ask/")
    ap.add_argument("--json", action="store_true", help="машинный вывод: {запрос: [карточки]}")
    ap.add_argument("--limit", type=int, default=8, metavar="N",
                    help="сколько запросов проверять (по умолчанию 8)")
    ap.add_argument("--no-semantic", action="store_true",
                    help="только по словам: видно вклад векторов")
    ap.add_argument("--save", action="store_true",
                    help="запомнить эту выдачу как точку сравнения")
    a = ap.parse_args()

    root = "."
    if not os.path.isdir(os.path.join(root, KB)):
        print("kb_retrieval: нет AuroraKnowledgeDB/ — запускайте из корня проекта",
              file=sys.stderr)
        return 1
    qs = a.query or queries(root, a.limit)
    if not qs:
        print("Проверять нечего: нет ни `meta/retrieval.txt`, ни разговоров в `meta/ask/`.\n"
              "Заведите список запросов по строке на вопрос — и отчёт станет сторожем "
              "ранжирования.")
        return 0

    was = {}
    if os.path.isfile(os.path.join(root, STATE)):
        try:
            with open(os.path.join(root, STATE), encoding="utf-8") as f:
                was = json.load(f)
        except ValueError:
            was = {}

    if a.json:
        print(json.dumps({q: [n for n, _ in rank(root, q, not a.no_semantic)] for q in qs},
                         ensure_ascii=False))
        return 0
    print(f"# Выдача по запросам — {len(qs)} шт.\n")
    print("Порядок карточек по каждому запросу. Меняется он от правок ранжирования, от "
          "новых карточек и от пересборки индекса — и это надо видеть, а не узнавать "
          "по жалобам.\n")
    now, moved = {}, 0
    for q in qs:
        rows = rank(root, q, not a.no_semantic)
        now[q] = [name for name, _ in rows]
        print(f"## {q}\n")
        if not rows:
            print("_ничего не найдено_\n")
        for i, (name, weight) in enumerate(rows, 1):
            mark = ""
            old = was.get(q) or []
            if old:
                if i == 1 and old and old[0] != name:
                    mark = f"  ← было первым: {old[0]}"
                    moved += 1
                elif name not in old:
                    mark = "  ← новая в выдаче"
            print(f"{i}. **{name}** · вес {weight}{mark}")
        print()

    known = [q for q in qs if q in was]
    fresh = [q for q in qs if q not in was]
    if known:
        print(f"---\n\nЗапросов, где сменилась первая карточка: **{moved}** из "
              f"{len(known)} сравнимых.")
        if not moved:
            print("Порядок по ним не менялся с прошлого прогона.")
    if fresh:
        # Молчание про неизмеренное — не подтверждение. Раньше отчёт писал «порядок не
        # менялся» про запрос, которого в точке сравнения не было вовсе. Найдено критиком.
        print(f"\nНе с чем сравнить (в прошлый раз их не спрашивали): {len(fresh)} — "
              + ", ".join(f"«{q}»" for q in fresh[:4])
              + ("…" if len(fresh) > 4 else ""))
    if not was:
        print("---\n\nТочки сравнения ещё нет: запустите с `--save`, и следующий прогон "
              "покажет разницу.")
    if a.save:
        os.makedirs(os.path.dirname(os.path.join(root, STATE)), exist_ok=True)
        with open(os.path.join(root, STATE), "w", encoding="utf-8") as f:
            json.dump(now, f, ensure_ascii=False, indent=1)
        print(f"\n✅ Выдача запомнена: {STATE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
