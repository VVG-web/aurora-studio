#!/usr/bin/env python3
"""context_quality.py - воспроизводимый тест «иголка в стогу» для контекста.

MVP не зовёт живую модель: он проверяет сам слой управления контекстом.
Стог из карточек проходит через хранилище, оглавление и сжатие, затем
детерминированный ответчик достаёт иглы через ctx_get. Метрики:

- recall: все иглы найдены;
- groundedness: каждое утверждение ответа имеет источник;
- completeness: все факты игл попали в ответ;
- p50/p95 времени контекстного пути.

Живой режим модели можно добавить отдельно; этот прогон должен быть
воспроизводимым в CI и не зависеть от шлюза.
"""
from __future__ import annotations

import aurora_common  # noqa: F401 - защита версии Python и UTF-8 для вывода

import argparse
import json
import tempfile
import time
from pathlib import Path

import context_budget as CB
import context_store as CS

NEEDLES = [
    {
        "id": "ALG-3.28",
        "text": "NEEDLE 1: перенос остатков между складами выполняется атомарно, срок 10 дней, журнал хранит 42 операции.",
        "facts": ["атомарно", "10 дней", "42"],
    },
    {
        "id": "ER.Tran",
        "text": "NEEDLE 2: таблица ER.Tran хранит 17 полей и ссылку на баланс, откат выполняется в той же транзакции.",
        "facts": ["17 полей", "ссылку на баланс", "откат"],
    },
    {
        "id": "REQ-042",
        "text": "NEEDLE 3: требование REQ-042 запрещает перевод при отрицательном остатке и требует журнал попыток.",
        "facts": ["отрицательном остатке", "журнал попыток"],
    },
    {
        "id": "AC-4.7.1",
        "text": "NEEDLE 4: критерий AC-4.7.1 требует показать оператору причину отказа и номер операции 42.",
        "facts": ["причину отказа", "номер операции 42"],
    },
]

FILLERS = [
    ("Заявка", "Заявка проходит согласование ролей и сохраняет историю статусов."),
    ("Договор", "Договор хранит реквизиты сторон и приложение с графиком платежей."),
    ("Платёж", "Платёж связан с балансом и отражается в журнале операций."),
    ("Склад", "Складской учёт ведёт остатки по номенклатуре и партиям."),
    ("Отчёт", "Отчёт собирает суммы по периодам и подразделениям."),
    ("Роль", "Роль описывает права пользователя и доступные действия."),
    ("Процесс", "Процесс описывает шаги согласования и точки контроля."),
    ("Справочник", "Справочник содержит коды и наименования объектов."),
]


def _card(card_id: str, text: str, needle: bool = False) -> str:
    trust = "knowledge" if needle else "draft"
    return (f"---\n\n## {card_id}\n\n**Доверие:** {trust}\n\n"
            f"Название: {card_id}\n\n## Описание\n\n{text}\n")


def _positions(total: int, count: int, placement: str) -> list[int]:
    count = max(1, min(count, total))
    if placement == "start":
        return list(range(count))
    if placement == "end":
        return list(range(total - count, total))
    if placement == "middle":
        start = max(0, (total - count) // 2)
        return list(range(start, start + count))
    # spread: равномерно, без дублей
    return sorted({min(total - 1, round((i + 0.5) * total / count)) for i in range(count)})


def build_haystack(cards: int = 600, needle_count: int = 1, placement: str = "spread"):
    """Собрать пак-стог и список игл для сценария."""
    total = max(needle_count, int(cards))
    needles = NEEDLES[:needle_count]
    pos = _positions(total, needle_count, placement)
    by_pos = {p: needles[i] for i, p in enumerate(pos)}
    out = ["# Context pack: needle test\n\n_Собран для проверки управления контекстом._\n"]
    ids = []
    for i in range(total):
        needle = by_pos.get(i)
        if needle:
            cid, text = needle["id"], needle["text"]
            ids.append(cid)
        else:
            name, body = FILLERS[i % len(FILLERS)]
            cid = f"{name}-{i:04d}"
            text = f"{body} Номер записи {i}, контрольная сумма {i * 7 % 1000}."
        out.append(_card(cid, text, needle=bool(needle)))
    return "\n".join(out), needles


def _answer(store: CS.ContextStore, context: str, needles: list[dict]) -> dict:
    """Детерминированный ответчик: игла видна в контексте, факты достаются через ctx_get."""
    claims, missing = [], []
    for needle in needles:
        visible = needle["id"] in context or needle["text"] in context
        if not visible:
            missing.append(needle["id"])
            continue
        got = store.get(needle["id"], max_chars=4000)
        if not got.get("ok"):
            missing.append(needle["id"])
            continue
        text = got["text"]
        facts = [f for f in needle["facts"] if f in text]
        if not facts:
            missing.append(needle["id"])
            continue
        claims.append({"text": " ".join(facts), "sources": [needle["id"]],
                       "confidence": "supported"})
    return {"claims": claims, "missing": missing}


def _score(store: CS.ContextStore, answer: dict, needles: list[dict]) -> dict:
    found = {c["sources"][0] for c in answer["claims"] if c.get("sources")}
    wanted = {n["id"] for n in needles}
    recall = len(found & wanted) / max(1, len(wanted))
    grounded = sum(1 for c in answer["claims"] if c.get("sources")
                   and all(store.item(s) for s in c["sources"]))
    groundedness = grounded / max(1, len(answer["claims"]))
    total_facts = sum(len(n["facts"]) for n in needles)
    got_facts = sum(len(n["facts"]) for n in needles if n["id"] in found)
    completeness = got_facts / max(1, total_facts)
    return {"recall": round(recall, 3), "groundedness": round(groundedness, 3),
            "completeness": round(completeness, 3), "found": sorted(found),
            "missing": answer.get("missing", [])}


def run_scenario(needle_count: int, placement: str, threshold: float, target_ratio: float,
                 cards: int = 600, window: int = 32768, repeats: int = 3) -> dict:
    pack, needles = build_haystack(cards, needle_count, placement)
    cfg = {
        "enabled": True,
        "compression": {"enabled": True, "threshold": threshold,
                        "threshold_tokens": 0, "target_ratio": target_ratio},
        "reserves": {"planner": 2000, "worker": 2000, "critic": 1000, "qa": 1000,
                     "compressor": 500, "tools": 1000, "safety": 500},
    }
    times, scores = [], []
    with tempfile.TemporaryDirectory(prefix="aurora-needle-") as tmp:
        store = CS.ContextStore(Path(tmp) / "context.sqlite")
        try:
            t0 = time.perf_counter()
            ids = store.put_pack(pack)
            planner = store.planner_pack(pack, max_full=8)
            compressed = store.maybe_compress(ids, window, cfg)
            context = planner
            if compressed:
                context += "\n\n## Конспект\n\n" + compressed["summary"]
            answer = _answer(store, context, needles)
            scores.append(_score(store, answer, needles))
            times.append(time.perf_counter() - t0)
            # Повторы нужны только для p50/p95 контекстного пути.
            for _ in range(max(0, repeats - 1)):
                t1 = time.perf_counter()
                store.planner_pack(pack, max_full=8)
                store.maybe_compress(ids, window, cfg)
                times.append(time.perf_counter() - t1)
        finally:
            store.close()
    times_sorted = sorted(times)
    def pct(p):
        if not times_sorted:
            return 0.0
        return times_sorted[min(len(times_sorted) - 1, int(round((len(times_sorted) - 1) * p)))]
    s = scores[0]
    return {
        "needles": needle_count, "placement": placement,
        "threshold": threshold, "target_ratio": target_ratio,
        "cards": cards, "window": window, "pack_tokens": CB.estimate_tokens(pack),
        "compressed": bool(compressed),
        "recall": s["recall"], "groundedness": s["groundedness"],
        "completeness": s["completeness"],
        "found": s["found"], "missing": s["missing"],
        "p50_ms": round(pct(0.50) * 1000, 2), "p95_ms": round(pct(0.95) * 1000, 2),
    }


def run_suite(cards: int = 600, repeats: int = 3,
              thresholds=(0.30, 0.50, 0.70), target_ratios=(0.35, 0.50, 0.65),
              placements=("start", "middle", "end", "spread"), window: int = 32768) -> dict:
    rows = []
    for n in (1, 2, 3, 4):
        for placement in placements:
            for threshold in thresholds:
                for ratio in target_ratios:
                    rows.append(run_scenario(n, placement, threshold, ratio,
                                             cards=cards, window=window, repeats=repeats))
    good = [r for r in rows if r["recall"] >= 1.0 and r["groundedness"] >= 0.9
            and r["completeness"] >= 0.9]
    best = min(good, key=lambda r: (r["p95_ms"], r["threshold"], r["target_ratio"])) if good else None
    return {"rows": rows, "best": best, "cards": cards, "repeats": repeats,
            "window": window,
            "thresholds": list(thresholds), "target_ratios": list(target_ratios)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Needle-in-a-haystack MVP для контекста Авроры")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    ap.add_argument("--cards", type=int, default=600)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--window", type=int, default=32768)
    ap.add_argument("--out", default="", help="записать JSON в файл")
    a = ap.parse_args()
    result = run_suite(cards=a.cards, repeats=a.repeats, window=a.window)
    if a.out:
        Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"cards={result['cards']} rows={len(result['rows'])}")
    for r in result["rows"][:8]:
        print(f"needles={r['needles']} {r['placement']} thr={r['threshold']} "
              f"ratio={r['target_ratio']} recall={r['recall']} "
              f"grounded={r['groundedness']} complete={r['completeness']} "
              f"p95={r['p95_ms']}ms compressed={r['compressed']}")
    print("best:", json.dumps(result["best"], ensure_ascii=False) if result["best"] else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
