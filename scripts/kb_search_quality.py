#!/usr/bin/env python3
"""kb_search_quality.py — качество поиска по базе: число, а не впечатление.

Панель: `ops:search-quality`

Отвечает на вопрос «работает ли поиск», не требуя, чтобы человек заранее разметил
правильные ответы. Мера — **самопоиск**: у карточки берётся её тезис (первый абзац тела,
то, что написал `agent:distill`), он подаётся в поиск как вопрос, и проверяется, вернётся
ли сама карточка. Разметка не нужна: правильный ответ известен по построению.

Почему это честная мера, а не самообман. Тезис — не тот текст, который лежит в индексе:
в вектор идёт заголовок, синонимы и до 1500 символов тела (`kb_embed.card_texts`), а
тезис короче и написан другими словами. Совпадение здесь означает, что поиск связывает
короткую формулировку смысла с полной карточкой — ровно то, что делает аналитик, когда
спрашивает базу своими словами.

Что мерим:

  R@1   доля карточек, нашедших себя первой строкой выдачи
  R@5   то же в первой пятёрке — с этим уже можно работать глазами
  MRR   средняя обратная позиция: 1.0 — всегда первая, 0.5 — в среднем вторая
  запас разрыв между собственной близостью и лучшим чужим ответом. Маленький
        запас значит, что выдача держится на волоске: любое пополнение базы её
        перетасует, и «стало хуже» будет не видно до жалобы человека

Отдельно — **эталонные вопросы** (`--golden`): вопросы человека из
`meta/golden_questions.md` со ссылкой на карточку-источник. Их мало, размечены они
руками, зато они меряют то, чего самопоиск не видит: понимает ли база вопрос, заданный
не её словами.

Прогон записывает результат в `meta/search-quality.json` и печатает **разницу с прошлым
разом**: одно число без истории говорит мало, а «R@1 упал с 0.81 до 0.62 после пополнения»
говорит всё.

  python3 .opencode/scripts/kb_search_quality.py                 # 200 карточек выборкой
  python3 .opencode/scripts/kb_search_quality.py --sample 500    # шире выборка
  python3 .opencode/scripts/kb_search_quality.py --golden        # ещё и эталонные вопросы
  python3 .opencode/scripts/kb_search_quality.py --apply         # записать замер в историю

Зависимостей нет: считает тот же `kb_embed`, которым живёт поиск.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kb_embed as EMB                                            # noqa: E402
from aurora_common import KB_ROOT, frontmatter, walk_md           # noqa: E402

HISTORY = os.path.join(KB_ROOT, "meta", "search-quality.json")
GOLDEN = os.path.join(KB_ROOT, "meta", "golden_questions.md")
TODAY = date.today().isoformat()
SEED = 20260902          # выборка одна и та же между прогонами: иначе разница врёт
TOP = 10                 # глубина выдачи, по которой считаем позицию


def thesis(text: str, limit: int = 400) -> str:
    """Тезис карточки — первый содержательный абзац тела.

    Не заголовок и не шапка: заголовок и так лежит в индексе, и спрашивать им — значит
    мерить совпадение строки с самой собой. Нужен именно пересказ смысла своими словами,
    который написал `agent:distill`.
    """
    body = text.split("---", 2)[-1]
    for para in re.split(r"\n\s*\n", body):
        s = " ".join(para.split())
        if not s or s.startswith(("#", ">", "|", "-", "*", "`", "<", "_")):
            continue
        if s.startswith("Заготовка") or "заготовка" in s[:40].lower():
            return ""    # заготовка не несёт смысла: спрашивать ею нечего
        return s[:limit]
    return ""


def cards_with_thesis(root: str = KB_ROOT) -> dict:
    """{имя карточки: тезис} — только знание с написанным тезисом."""
    out = {}
    for path in walk_md(root, skip_service=True, skip_archive=True):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        fm = frontmatter(text)
        if (fm.get("kind") or "").strip().strip('"') != "knowledge":
            continue
        if not (fm.get("distilled") or "").strip():
            continue
        t = thesis(text)
        if len(t) >= 60:            # слишком короткий тезис — не вопрос, а подпись
            out[os.path.basename(path)[:-3]] = t
    return out


def rank_of(name: str, hits: list) -> int:
    """Позиция карточки в выдаче, считая с единицы. 0 — не нашлась вовсе."""
    for i, (found, _score) in enumerate(hits, 1):
        if found == name:
            return i
    return 0


def measure(pairs: list, cfg: dict, model: str, say=print) -> dict:
    """Прогнать самопоиск по парам (имя, вопрос). → сводка."""
    ranks, margins, misses = [], [], []
    for i, (name, question) in enumerate(pairs, 1):
        hits = EMB.search(question, cfg, model, limit=TOP)
        if not hits:
            misses.append((name, "поиск ничего не вернул"))
            ranks.append(0)
            continue
        r = rank_of(name, hits)
        ranks.append(r)
        own = next((s for n, s in hits if n == name), None)
        best_other = next((s for n, s in hits if n != name), None)
        if own is not None and best_other is not None:
            margins.append(own - best_other)
        # В список попадают все, кто не вошёл в первую пятёрку: карточка на седьмом
        # месте так же не найдена, как и не найденная вовсе. Список обязан совпадать с
        # R@5 — иначе он пуст при R@1 = 0.12, и мера расходится с тем, что показывает.
        if r == 0 or r > 5:
            where = f"первой пришла «{hits[0][0]}»" if r == 0 else f"только на {r}-м месте"
            misses.append((name, where))
        if i % 25 == 0:
            say(f"  {i}/{len(pairs)} · R@1 пока "
                f"{sum(1 for x in ranks if x == 1) / len(ranks):.2f}")
    n = len(ranks) or 1
    return {
        "карточек": len(ranks),
        "R@1": round(sum(1 for r in ranks if r == 1) / n, 3),
        "R@5": round(sum(1 for r in ranks if 1 <= r <= 5) / n, 3),
        "MRR": round(sum(1 / r for r in ranks if r) / n, 3),
        "запас": round(statistics.median(margins), 4) if margins else 0.0,
        "не нашлись": misses,
    }


def golden_pairs(root: str = KB_ROOT) -> list:
    """[(карточка, вопрос)] из эталона: вопрос человека и карточка, где ответ.

    Формат строки эталона свободный — берём то, что можно разобрать однозначно:
    текст вопроса и ссылку `[[Карточка]]` в той же строке.
    """
    out = []
    try:
        text = open(GOLDEN, encoding="utf-8", errors="ignore").read()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.search(r"\[\[([^\]|#]+)", line)
        if not m:
            continue
        q = re.sub(r"\[\[[^\]]*\]\]", "", line).strip(" -*|→#").strip()
        if len(q) >= 12:
            out.append((m.group(1).strip(), q))
    return out


def history() -> dict:
    try:
        with open(HISTORY, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"runs": []}


def diff_line(now: dict, prev: dict) -> str:
    """Человеческая разница с прошлым замером. Одно число без истории говорит мало."""
    if not prev:
        return "первый замер — сравнивать не с чем"
    out = []
    for key in ("R@1", "R@5", "MRR"):
        was, is_ = prev.get(key), now.get(key)
        if was is None or is_ is None:
            continue
        d = round(is_ - was, 3)
        if abs(d) >= 0.01:
            out.append(f"{key} {was} → {is_} ({d:+})")
    return " · ".join(out) or "без заметных изменений"


def main() -> int:
    ap = argparse.ArgumentParser(description="Качество поиска по базе знаний")
    ap.add_argument("--sample", type=int, default=200,
                    help="сколько карточек взять в выборку (0 — все)")
    ap.add_argument("--golden", action="store_true",
                    help="ещё и эталонные вопросы из meta/golden_questions.md")
    ap.add_argument("--apply", action="store_true",
                    help="записать замер в историю meta/search-quality.json")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"kb_search_quality: нет {KB_ROOT}/ — запускайте из корня проекта",
              file=sys.stderr)
        return 1

    idx = EMB.load_index()
    if not idx.get("cards"):
        print("Индекса нет. Соберите: `kb:embed --apply` — без него поиск идёт по словам,\n"
              "и мерить в нём нечего.", file=sys.stderr)
        return 1

    import aurora_common as AC
    cfg = AC.parse_config(AC.raw_config()) if hasattr(AC, "parse_config") else None
    if cfg is None:
        import agent_core as AG
        cfg = AG.parse_config(AG.raw_config())
    model = cfg["embed"]["model"]

    print(f"# Качество поиска — {TODAY}\n")

    have = cards_with_thesis()
    known = set(idx["cards"])
    pairs = [(n, q) for n, q in have.items() if n in known]
    if not pairs:
        print("Карточек с тезисом в индексе нет. Сначала `agent:distill --apply`,\n"
              "затем `kb:embed --apply`: мерить поиск по заготовкам бессмысленно.")
        return 1

    rng = random.Random(SEED)
    if a.sample and a.sample < len(pairs):
        pairs = rng.sample(sorted(pairs), a.sample)
    else:
        pairs = sorted(pairs)

    print(f"Самопоиск: {len(pairs)} карточек из {len(have)} с тезисом "
          f"(в индексе {len(known)})")
    print("Вопрос — тезис карточки; правильный ответ — она сама.\n")
    res = measure(pairs, cfg, model)

    prev_runs = history().get("runs") or []
    prev = prev_runs[-1] if prev_runs else {}
    print(f"\n| Мера | Значение | Что означает |")
    print("|---|---|---|")
    print(f"| R@1 | **{res['R@1']}** | нашла себя первой строкой |")
    print(f"| R@5 | **{res['R@5']}** | нашла себя в первой пятёрке |")
    print(f"| MRR | **{res['MRR']}** | 1.0 — всегда первая, 0.5 — в среднем вторая |")
    print(f"| запас | {res['запас']} | отрыв от лучшего чужого ответа |")
    print(f"\nС прошлым замером: {diff_line(res, prev)}")

    if res["не нашлись"]:
        print(f"\n## Не нашли себя в первой пятёрке: {len(res['не нашлись'])}\n")
        print("Карточка, которую не находит собственный тезис, не найдётся и по вопросу "
              "человека. Смотреть стоит на них, а не на среднее.\n")
        for name, why in res["не нашлись"][:15]:
            print(f"- `{name}` — {why}")
        if len(res["не нашлись"]) > 15:
            print(f"- … ещё {len(res['не нашлись']) - 15}")

    gold = {}
    if a.golden:
        gp = [(n, q) for n, q in golden_pairs() if n in known]
        print(f"\n## Эталонные вопросы: {len(gp)}\n")
        if not gp:
            print("В `meta/golden_questions.md` нет строк вида «вопрос … [[Карточка]]»,\n"
                  "либо названные карточки из базы ушли. Самопоиск меряет связь тезиса с\n"
                  "карточкой, эталон — понимание вопроса, заданного не словами базы.")
        else:
            gold = measure(gp, cfg, model, say=lambda *_: None)
            print(f"R@1 **{gold['R@1']}** · R@5 **{gold['R@5']}** · MRR **{gold['MRR']}**")
            for name, why in gold["не нашлись"][:10]:
                print(f"- `{name}` — {why}")

    if a.apply:
        h = history()
        h.setdefault("runs", []).append({
            "дата": TODAY, "карточек": res["карточек"],
            "R@1": res["R@1"], "R@5": res["R@5"], "MRR": res["MRR"],
            "запас": res["запас"], "не нашлись": len(res["не нашлись"]),
            **({"эталон_R@1": gold["R@1"]} if gold else {}),
        })
        h["runs"] = h["runs"][-50:]
        os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
        with open(HISTORY, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=1)
        print(f"\n✅ Замер записан: {HISTORY} (прогонов в истории: {len(h['runs'])})")
    else:
        print("\n(dry-run) В историю не записано. Записать: `--apply`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
