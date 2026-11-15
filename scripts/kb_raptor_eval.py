#!/usr/bin/env python3
"""kb_raptor_eval.py — замер обзора базы на списке вопросов.

Панель: строка «Обзор базы» в разделе «Спросить».

Замер отвечает на два разных вопроса:

1. Считает ли классификатор вопрос общим. Если «как настроить систему» уйдёт
   в обзор, ответ станет хуже, чем обычный поиск.
2. Находит ли обзор правильный узел. Для пересказов это главная метрика:
   человек не называет раздел точно, а спрашивает «какие системы есть».

Вопросы лежат в `AuroraKnowledgeDB/meta/raptor_questions.json` (или в файле,
указанном `--questions`). Формат: список объектов с `question`,
`expected_node`, необязательными `kind` (`exact`/`paraphrase`), `expected_cards`
и `global`. Замер ничего не меняет в карточках: только читает индекс и пишет
`meta/raptor_metrics.json`.

  python3 .aurora/scripts/kb_raptor_eval.py --questions ... --apply
  python3 .aurora/scripts/kb_raptor_eval.py --questions ... --json
  python3 .aurora/scripts/kb_raptor_eval.py --questions ... --gate

`--gate` возвращает код 1, если пересказы не набрали 75 из 100. Это условие
включения обзора в обычный поиск, а не ежедневная проверка: обычный запуск
без `--gate` всегда успешен.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kb_raptor as R  # noqa: E402
from aurora_common import utc_stamp  # noqa: E402

DEFAULT_CANDIDATES = (
    os.path.join(R.KB_ROOT, "meta", "raptor_questions.json"),
    os.path.join("Development", "quality-checks", "raptor_global_50.json"),
    os.path.join("Development", "quality-checks", "raptor_questions.json"),
)
METRICS = os.path.join(R.KB_ROOT, "meta", "raptor_metrics.json")
TARGET = 0.75


def default_questions() -> str:
    for path in DEFAULT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return DEFAULT_CANDIDATES[0]


def load_questions(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        raw = json.loads(f.read())
    if not isinstance(raw, list):
        raise ValueError("файл вопросов должен быть списком")
    out = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        row = dict(item)
        row.setdefault("id", i)
        row.setdefault("expected_node", "")
        row.setdefault("expected_cards", [])
        out.append(row)
    return out


def _title(node: dict | None) -> str:
    return str((node or {}).get("title") or "").strip()


def _split(row: dict, paraphrase_from: int) -> str:
    kind = str(row.get("kind") or "").strip().lower()
    if kind in ("exact", "paraphrase"):
        return kind
    try:
        return "paraphrase" if int(row.get("id") or 0) >= paraphrase_from > 0 else "exact"
    except (TypeError, ValueError):
        return "exact"


def evaluate(questions: list, *, cfg: dict | None = None, vectors: bool = False,
             paraphrase_from: int = 0, limit: int = 5) -> dict:
    rows = []
    for q in questions:
        text = str(q.get("question") or "").strip()
        expected = str(q.get("expected_node") or "").strip()
        found = R.rank(text, limit=limit, cfg=cfg, vectors=vectors)
        top = found[0][1] if found else None
        info = R.classify_global(text)
        got_title = _title(top)
        correct = bool(expected) and got_title.casefold() == expected.casefold()
        cards = set((top or {}).get("cards") or [])
        wanted = {str(x) for x in (q.get("expected_cards") or [])}
        rows.append({
            "id": q.get("id"),
            "question": text,
            "kind": _split(q, paraphrase_from),
            "expected_node": expected,
            "top_node": got_title,
            "top_id": (top or {}).get("id") or "",
            "top_correct": correct,
            "expected_cards_found": sorted(wanted & cards),
            "expected_cards_missing": sorted(wanted - cards),
            "global_expected": q.get("global") if isinstance(q.get("global"), bool) else None,
            "global_actual": bool(info.get("global")),
            "global_why": info.get("why") or "",
            "score": round(found[0][0], 3) if found else 0.0,
        })
    return summarise(rows, vectors=vectors)


def _rate(n: int, d: int) -> float:
    return round(n / d, 3) if d else 0.0


def summarise(rows: list, *, vectors: bool = False) -> dict:
    def group(kind: str) -> list:
        return [r for r in rows if r["kind"] == kind]

    exact, para = group("exact"), group("paraphrase")
    both = exact + para
    # Классификатор: только там, где в файле явно сказано, общий вопрос или
    # точный. Без метки это не ошибка классификатора, а отсутствие разметки.
    labeled = [r for r in rows if isinstance(r["global_expected"], bool)]
    tp = sum(1 for r in labeled if r["global_expected"] and r["global_actual"])
    fp = sum(1 for r in labeled if not r["global_expected"] and r["global_actual"])
    fn = sum(1 for r in labeled if r["global_expected"] and not r["global_actual"])
    tn = sum(1 for r in labeled if not r["global_expected"] and not r["global_actual"])
    para_top = _rate(sum(1 for r in para if r["top_correct"]), len(para))
    return {
        "generated_at": utc_stamp(),
        "questions": len(rows),
        "vectors": bool(vectors),
        "top1": {
            "all": _rate(sum(1 for r in both if r["top_correct"]), len(both)),
            "exact": _rate(sum(1 for r in exact if r["top_correct"]), len(exact)),
            "paraphrase": para_top,
            "n_exact": len(exact),
            "n_paraphrase": len(para),
        },
        "classifier": {
            "n": len(labeled),
            "accuracy": _rate(tp + tn, len(labeled)) if labeled else None,
            "precision": _rate(tp, tp + fp),
            "recall": _rate(tp, tp + fn),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        },
        "gate": {
            "target": TARGET,
            "paraphrase_top1": para_top,
            "passed": bool(para) and para_top >= TARGET,
            "why": ("пересказов нет — порог не проверить" if not para else
                    "пересказы набрали " + str(round(para_top * 100)) + " из 100"),
        },
        "rows": rows,
    }


def save(metrics: dict, path: str = METRICS) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Замер обзора базы на списке вопросов")
    ap.add_argument("--questions", default=default_questions(), metavar="ПУТЬ")
    ap.add_argument("--paraphrase-from", type=int, default=0, metavar="N",
                    help="считать вопросы с N-го пересказами, если в файле нет kind")
    ap.add_argument("--vectors", action="store_true",
                    help="добавить готовый семантический индекс")
    ap.add_argument("--apply", action="store_true", help="записать meta/raptor_metrics.json")
    ap.add_argument("--gate", action="store_true", help="код 1, если пересказы ниже 75 из 100")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--quiet", action="store_true", help="не печатать сводку")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.questions):
        print(f"kb_raptor_eval: нет файла вопросов {a.questions}", file=sys.stderr)
        return 2
    try:
        questions = load_questions(a.questions)
    except (OSError, ValueError) as exc:
        print(f"kb_raptor_eval: {exc}", file=sys.stderr)
        return 2
    if not questions:
        print("kb_raptor_eval: в файле нет вопросов", file=sys.stderr)
        return 2
    cfg = None
    if a.vectors:
        try:
            import agent_core as AG
            cfg = AG.config()
        except Exception:  # noqa: BLE001 — без настройки остаётся словесный поиск
            cfg = None
    paraphrase_from = a.paraphrase_from
    if not paraphrase_from and os.path.basename(a.questions) == "raptor_global_50.json":
        # Этот набор собран как 30 точных и 20 пересказов. В нём нет поля
        # kind, чтобы не переписывать исторический файл; делится по номеру.
        paraphrase_from = 31
    metrics = evaluate(questions, cfg=cfg, vectors=a.vectors,
                       paraphrase_from=paraphrase_from)
    metrics["source"] = a.questions
    metrics["paraphrase_from"] = paraphrase_from
    if a.apply:
        save(metrics)
    if a.json:
        print(json.dumps(metrics, ensure_ascii=False))
    elif not a.quiet:
        t = metrics["top1"]
        c = metrics["classifier"]
        print(f"вопросов: {metrics['questions']} · "
              f"первый узел: все {t['all']:.0%}, точные {t['exact']:.0%}, "
              f"пересказы {t['paraphrase']:.0%}")
        if c.get("n"):
            print(f"классификатор: точность {c['accuracy']:.0%}, "
                  f"полнота {c['recall']:.0%}, ложных общих {c['fp']}")
        else:
            print("классификатор: размеченных вопросов нет")
        print("порог: " + metrics["gate"]["why"] +
              (" — пройден" if metrics["gate"]["passed"] else " — не пройден"))
        if a.apply:
            print("метрики: " + METRICS)
    return 0 if (metrics["gate"]["passed"] or not a.gate) else 1


if __name__ == "__main__":
    raise SystemExit(main())
