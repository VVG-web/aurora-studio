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
  запас разрыв в весе выдачи между своей карточкой и лучшим чужим ответом.
        Маленький запас значит, что выдача держится на волоске: любое пополнение
        базы её перетасует, и «стало хуже» будет не видно до жалобы человека.
        Вес — доля от лучшего в этом запросе, а не косинус: до 1.100.38 запас
        считался в косинусах, и числа тех прогонов с нынешними несравнимы

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

Считает **той же выборкой, которой отвечают человеку** — гибридной `ctx_pack.fuse`:
слова с весом по редкости плюс близость по смыслу. До 1.100.38 замер ходил в
`kb_embed.search`, то есть мерил чистые вектора: датчик стоял не на том пути, показывал
качество индекса вместо качества ответа и не увидел поломки в сложении двух сигналов.
История замеров с той поры несравнима и начата заново.
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

import agent_core as AG                                           # noqa: E402
import kb_embed as EMB                                            # noqa: E402
from aurora_common import (KB_ROOT, frontmatter, is_placeholder,  # noqa: E402
                           walk_md)

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


def rank_of(names, hits: list) -> int:
    """Лучшая позиция среди годных карточек, считая с единицы. 0 — ни одной нет.

    Годных бывает несколько, и это не поблажка. Знание в живой базе лежит в нескольких
    карточках: справочник, процесс, где он применяется, и разбор частного случая — все
    трое отвечают на вопрос верно. Требовать одну конкретную значит мерить не поиск, а
    угадывание имени: выдача законно вернёт соседа, и замер назовёт это провалом.
    """
    ok = {names} if isinstance(names, str) else set(names)
    for i, (found, _score) in enumerate(hits, 1):
        if found in ok:
            return i
    return 0


def ranked(question: str, cfg: dict, model: str) -> list:
    """[(имя, вес)] — выдача ТОЙ ЖЕ выборкой, которой отвечают человеку.

    Замер годами мерил `kb_embed.search` — чистые вектора. А отвечает «Спросить»
    гибридной выборкой `ctx_pack`: слова с весом по редкости плюс близость по смыслу.
    Датчик стоял не на том пути: он показывал качество индекса, а не качество ответа, и
    поломка в сложении двух сигналов (1.100.38) была ему не видна вовсе.
    """
    import ctx_pack as P
    global _CARDS
    if _CARDS is None:
        _CARDS = P.load_cards()
        P.measure_rarity(_CARDS)
    close = P.semantic(question, TOP * 4)
    # Карты содержания и заготовки из выдачи убираем: в паке их отсекает отбор по
    # статусу (`MODE_STATUSES`), и замер обязан мерить то же самое. Иначе он показывает
    # промах там, где человеку ответили верно, — на живой базе карточку обошла страница
    # «Пустышки», то есть навигация, которую в ответ всё равно не подставят.
    out = []
    for w, c in P.fuse(_CARDS, question, close):
        if (c.status or "").strip() == "index" or is_placeholder(c.fm, c.text):
            continue
        out.append((c.stem, round(w, 4)))
        if len(out) >= TOP:
            break
    return out


_CARDS = None            # обзор базы для выборки: собирается один раз на прогон


def measure(pairs: list, cfg: dict, model: str, say=print) -> dict:
    """Прогнать самопоиск по парам (имя, вопрос). → сводка."""
    ranks, margins, misses = [], [], []
    for i, (names, question) in enumerate(pairs, 1):
        ok = {names} if isinstance(names, str) else set(names)
        label = " / ".join(sorted(ok))
        hits = ranked(question, cfg, model)
        if not hits:
            misses.append((label, "поиск ничего не вернул"))
            ranks.append(0)
            continue
        r = rank_of(ok, hits)
        ranks.append(r)
        own = next((s for n, s in hits if n in ok), None)
        best_other = next((s for n, s in hits if n not in ok), None)
        if own is not None and best_other is not None:
            margins.append(own - best_other)
        # В список попадают все, кто не вошёл в первую пятёрку: карточка на седьмом
        # месте так же не найдена, как и не найденная вовсе. Список обязан совпадать с
        # R@5 — иначе он пуст при R@1 = 0.12, и мера расходится с тем, что показывает.
        if r == 0 or r > 5:
            where = f"первой пришла «{hits[0][0]}»" if r == 0 else f"только на {r}-м месте"
            misses.append((label, where))
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
    """[(годные карточки, вопрос)] из эталона: вопрос человека и где лежит ответ.

    Эталон ведётся таблицей `| # | Вопрос | Эталон | [[Карточка]] … |`, и брать «всю
    строку без ссылок» нельзя: рядом с вопросом лежит **готовый ответ**. Спрашивать базу
    вопросом вместе с ответом — мерить не поиск, а собственную подсказку: эталон
    пересказывает тело карточки, и попадание выходит само собой. Берём колонку вопроса.

    Ссылок в строке может быть несколько, и годится **любая**: знание в живой базе лежит
    в нескольких карточках сразу, и требовать одну — мерить угадывание имени.
    """
    out = []
    try:
        text = open(GOLDEN, encoding="utf-8", errors="ignore").read()
    except OSError:
        return out
    for line in text.splitlines():
        cards = [c.strip() for c in re.findall(r"\[\[([^\]|#]+)", line)]
        if not cards:
            continue
        if "|" in line:
            # Строка таблицы. Вопросом считается только пронумерованная: в файле живут и
            # другие таблицы — например реестр найденных в базе противоречий, — и они
            # тоже полны ссылок. Без номера такая строка уехала бы в замер как вопрос,
            # и мера считала бы то, чего человек в неё не клал.
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3 or not cells[0].strip("# ").isdigit():
                continue
            # колонка вопроса — первая после номера, где есть текст, а не ссылки
            q = next((c for c in cells[1:] if len(c) >= 12 and "[[" not in c), "")
        else:
            q = re.sub(r"\[\[[^\]]*\]\]", "", line).strip(" -*|→#").strip()
        if len(q) >= 12:
            out.append((tuple(dict.fromkeys(cards)), q))
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

    # Индекса нет — мерить всё равно есть что: выборка гибридная и без векторов идёт по
    # словам, ровно так же, как в этом случае отвечают человеку. Замер обязан показывать
    # то, что человек получит, а не отказываться, пока не собран индекс.
    idx = EMB.load_index()
    if not idx.get("cards"):
        print("Индекса нет: меряем выдачу по словам — ровно ту, которую сейчас получает\n"
              "человек. Соберите `kb:embed --apply`, чтобы мерить полную.", file=sys.stderr)

    # Конфиг читаем тем же кодом, что и поиск: модель эмбеддингов обязана совпасть с
    # той, которой собран индекс, иначе `kb_embed.search` молча вернёт пустоту.
    cfg = AG.parse_config(AG.raw_config())
    model = cfg["embed"]["model"]

    # Отказ вместо нуля. `kb_embed.search` на чужой модели возвращает пустоту молча —
    # и замер напечатал бы «R@1 0.0», то есть «поиск сломан», хотя сломана настройка.
    # Ноль, полученный не измерением, хуже отсутствия числа: его понесут чинить базу.
    if idx.get("model") and idx["model"] != model:
        print(f"Индекс собран моделью «{idx['model']}», а в конфиге «{model}».\n"
              "Мерить нечего: поиск на чужой модели не отвечает вовсе, и замер показал бы\n"
              "ноль вместо качества. Пересоберите индекс (`kb:embed --apply`) либо верните\n"
              "в AURORA_EMBED_MODEL ту модель, которой он собран.", file=sys.stderr)
        return 1

    print(f"# Качество поиска — {TODAY}\n")

    # Карточки НЕ фильтруются по векторному индексу. Пока замер ходил в `kb_embed`,
    # это было обязательно: чего нет в индексе, то не находится вовсе. Теперь выборка
    # гибридная, карточка вне индекса находится словами — а молча выкидывать её из
    # замера значит прятать ровно ту дыру, ради которой замер и заведён.
    pairs = list(cards_with_thesis().items())
    if not pairs:
        # Молодая база — не поломка. Шаг стоит в маршруте «Починить базу», и красный
        # здесь означал бы «что-то сломано», хотя мерить просто нечего: тезисов ещё не
        # написали. Ложная тревога в маршруте дороже отсутствующего числа.
        print("Мерить нечего: карточек с написанным тезисом в индексе нет.\n"
              "Сначала `agent:distill --apply`, затем `kb:embed --apply` — по заготовкам\n"
              "качество поиска не считается.")
        return 0

    rng = random.Random(SEED)
    if a.sample and a.sample < len(pairs):
        pairs = rng.sample(sorted(pairs), a.sample)
    else:
        pairs = sorted(pairs)

    print(f"Самопоиск: {len(pairs)} карточек с тезисом"
          + (f" (в векторном индексе {len(idx['cards'])})" if idx.get("cards")
             else " · индекса нет, выдача по словам"))
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
        all_gp = golden_pairs()
        # Строка годится, пока в индексе есть ХОТЬ ОДНА из названных карточек: остальные
        # могли переехать, и это не повод выбрасывать вопрос целиком.
        gp = [(tuple(n for n in names if n in known), q)
              for names, q in all_gp if any(n in known for n in names)]
        lost = sorted({n for names, _q in all_gp for n in names if n not in known})
        print(f"\n## Эталонные вопросы: {len(gp)}\n")
        if lost:
            # Эталон стареет вместе с базой: карточку переименовали или разрезали, а
            # строка осталась. Молча выкинуть её значит мерить по остатку и не сказать
            # об этом; ошибка в эталоне ищется дольше всего именно потому, что не видна.
            print(f"Эталон ссылается на {len(lost)} карточек, которых в индексе нет — "
                  "он отстал\nот базы (строка идёт в замер, если жива хоть одна её "
                  "карточка): "
                  + ", ".join("`" + n + "`" for n in lost[:8])
                  + (" …" if len(lost) > 8 else "") + "\n")
        if not gp:
            print("В `meta/golden_questions.md` нет строк вида «вопрос … [[Карточка]]»,\n"
                  "либо названные карточки из базы ушли. Самопоиск меряет связь тезиса с\n"
                  "карточкой, эталон — понимание вопроса, заданного не словами базы.")
        else:
            gold = measure(gp, cfg, model, say=lambda *_: None)
            print(f"R@1 **{gold['R@1']}** · R@5 **{gold['R@5']}** · MRR **{gold['MRR']}**")
            for name, why in gold["не нашлись"][:10]:
                print(f"- `{name}` — {why}")
            # Число здесь читается иначе, чем у самопоиска, и об этом надо сказать прямо.
            # У самопоиска правильный ответ известен по построению — расхождение всегда
            # про поиск. В эталоне правильный ответ назвал человек, и когда база растёт,
            # знание переезжает в карточку точнее прежней: поиск отдаёт её, а эталон
            # по-прежнему ждёт старую. Тогда падает не поиск, а срок годности эталона.
            if gold["R@1"] < res["R@1"] - 0.15:
                print("\nЭталон отстаёт от самопоиска — сначала прочитайте, ЧТО пришло "
                      "первым.\nЕсли пришедшая карточка отвечает на вопрос точнее "
                      "названной в эталоне,\nустарел эталон, а не поиск: база с тех пор "
                      "нарезала знание мельче.\nЕсли же пришло не по делу — это "
                      "настоящая слабость: короткий вопрос\nне словами базы находит "
                      "тему, а не карточку с ответом.")

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
