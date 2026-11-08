#!/usr/bin/env python3
"""quality_eval.py - золотой набор и регрессионный гейт качества ответа и артефакта.

Метрики названы как в RAGAS и считаются детерминированно, без сети:

- Faithfulness: доля утверждений ответа, подтверждённых паком;
- Answer Relevancy: F1 пересечения слов вопроса и ответа;
- Context Recall: доля обязательных фактов, попавших в пак;
- Context Precision: доля выданных карточек, которые были нужны;
- Artifact Completeness: доля обязательных фактов, попавших в артефакт.

Если установлен `ragas`, CLI может показать его версию и доступность, но офлайн-гейт
не зависит от пакета: иначе проверка качества ломалась бы на машине без RAGAS. Это
явно записано в отчёте (`ragas.installed`), а не выдаётся за живой прогон RAGAS.

  python3 scripts/quality_eval.py                 # отчёт по золотому набору
  python3 scripts/quality_eval.py --gate          # абсолютные и относительные пороги
  python3 scripts/quality_eval.py --write-baseline --baseline Development/QUALITY_BASELINE.json
"""
from __future__ import annotations

import aurora_common  # noqa: F401 - защита версии Python и UTF-8 для вывода

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
GOLDEN = KIT / "scripts" / "quality_golden.json"
DEFAULT_BASELINE = KIT / "scripts" / "quality_baseline.json"
METRICS = ("faithfulness", "answer_relevancy", "context_recall",
           "context_precision", "artifact_completeness")
THRESHOLDS = {
    "faithfulness": 0.90,
    "answer_relevancy": 0.35,
    "context_recall": 0.80,
    "context_precision": 0.50,
    "artifact_completeness": 0.80,
}
RELATIVE_DROP = 0.05
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().replace("ё", "е").split())


def _version() -> str:
    try:
        return (KIT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _commit() -> str:
    try:
        return subprocess.run(["git", "-C", str(KIT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(_norm(text)) if len(w) >= 3}


def _claims(text: str) -> list[str]:
    """Утверждения ответа: строки и предложения без заголовков и пустой разметки."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", "```", "---", ">")):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        for part in re.split(r"(?<=[.!?])\s+", line):
            part = " ".join(part.split())
            if len(_tokens(part)) >= 2:
                out.append(part)
    return out


def _supported(claim: str, context: str) -> bool:
    low = _norm(context)
    refs = re.findall(r"\[\[([^\]|#]+)", claim)
    if refs and any(_norm(r) in low for r in refs):
        return True
    ct = _tokens(claim)
    if not ct:
        return False
    return sum(1 for t in ct if t in _tokens(context)) / len(ct) >= 0.45


def faithfulness(answer: str, context: str) -> float:
    claims = _claims(answer)
    if not claims:
        return 0.0
    return round(sum(1 for c in claims if _supported(c, context)) / len(claims), 4)


def answer_relevancy(question: str, answer: str) -> float:
    q, a = _tokens(question), _tokens(answer)
    if not q or not a:
        return 0.0
    both = len(q & a)
    if not both:
        return 0.0
    precision = both / len(a)
    recall = both / len(q)
    return round(2 * precision * recall / (precision + recall), 4)


def context_recall(facts: list[str], context: str) -> float:
    facts = [f for f in facts or [] if str(f).strip()]
    if not facts:
        return 1.0
    low = _norm(context)
    return round(sum(1 for f in facts if _norm(f) in low) / len(facts), 4)


def _pack_cards(pack: str) -> list[str]:
    return [x.strip() for x in re.findall(r"(?m)^##\s+(.+?)\s*$", str(pack or ""))]


def context_precision(case: dict, cards: list[str] | None = None) -> float:
    if cards is None:
        retrieved = [str(c) for c in (case.get("cards") or _pack_cards(case.get("pack") or ""))]
    else:
        retrieved = [str(c) for c in cards]
    needed = {_norm(c) for c in case.get("relevant_cards") or []}
    if not retrieved:
        return 0.0
    if not needed:
        return 1.0
    return round(sum(1 for c in retrieved if _norm(c) in needed) / len(retrieved), 4)


def artifact_completeness(facts: list[str], answer: str) -> float:
    facts = [f for f in facts or [] if str(f).strip()]
    if not facts:
        return 1.0
    low = _norm(answer)
    return round(sum(1 for f in facts if _norm(f) in low) / len(facts), 4)


def evaluate_case(case: dict, answer_fn=None) -> dict:
    """Один кейс: ответ получает `answer_fn(case)`, метрики считает код."""
    if answer_fn is None:
        got = {"answer": case.get("reference") or "",
               "context": case.get("pack") or "",
               "cards": case.get("cards") or []}
    else:
        got = answer_fn(case) or {}
        if isinstance(got, str):
            got = {"answer": got}
    answer = str(got.get("answer") or "")
    context = str(got["context"]) if "context" in got else str(case.get("pack") or "")
    cards = (list(got.get("cards") or []) if "cards" in got
             else list(case.get("cards") or []))
    facts = [str(x) for x in case.get("facts") or []]
    metrics = {
        "faithfulness": faithfulness(answer, context),
        "answer_relevancy": answer_relevancy(case.get("question") or "", answer),
        "context_recall": context_recall(facts, context),
        "context_precision": context_precision(case, cards),
        "artifact_completeness": artifact_completeness(facts, answer),
    }
    return {"id": case.get("id"), "metrics": metrics, "answer": answer,
            "missing_facts": [f for f in facts if _norm(f) not in _norm(answer)]}


def _ragas_status() -> dict:
    try:
        import ragas  # noqa: F401
        return {"installed": True, "version": str(getattr(ragas, "__version__", "unknown")),
                "note": "RAGAS установлен; офлайн-метрики ниже не заменяются сетевым вызовом."}
    except Exception:  # noqa: BLE001 - отсутствие пакета не должно ломать гейт
        return {"installed": False, "version": "",
                "note": "RAGAS не установлен; используются детерминированные определения метрик."}


def evaluate(cases: list[dict], *, answer_fn=None, source: str = "reference") -> dict:
    rows = [evaluate_case(c, answer_fn) for c in cases]
    metrics = {m: round(sum(r["metrics"][m] for r in rows) / len(rows), 4) if rows else 0.0
               for m in METRICS}
    return {"version": _version(), "commit": _commit(), "source": source,
            "cases": rows, "metrics": metrics,
            "ragas": _ragas_status()}


def pipeline_answer(case: dict) -> dict:
    """Детерминированный прогон через `answer_core`: проверяет сам путь, а не только метрику.

    Модель подменена эталоном кейса: сеть не нужна, но поиск, сборка ответа, проверки и
    контракт ядра проходят по-настоящему.
    """
    import answer_core as AC

    def fake(cfg, role, messages, **kw):
        return {"ok": True, "text": case.get("reference") or "", "backend": 1,
                "model": "golden-oracle", "log": [], "seconds": 0.0}

    res = AC.answer(case.get("question") or "", cfg={"request_timeout": 20}, cwd=".",
                    call=fake, pack=case.get("pack") or "", prompt="",
                    prepare=False, momus=False)
    return {"answer": res.get("answer") or "",
            "context": case.get("pack") or "",
            "cards": res.get("cards") or case.get("cards") or []}


def load_golden(path: str | Path = GOLDEN) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"version": "unknown", "cases": data}
    if not data.get("cases"):
        raise ValueError("в золотом наборе нет cases")
    return data


def load_baseline(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def check_gate(report: dict, baseline: dict | None = None, *,
               thresholds: dict | None = None, relative: float = RELATIVE_DROP) -> dict:
    """Абсолютные пороги и падение относительно baseline. → {ok, failures}."""
    thresholds = thresholds or THRESHOLDS
    failures = []
    for metric, floor in thresholds.items():
        current = float((report.get("metrics") or {}).get(metric) or 0.0)
        if current < floor:
            failures.append(f"{metric}: {current:.3f} < абсолютного порога {floor:.3f}")
        base = ((baseline or {}).get("metrics") or {}).get(metric)
        if base is not None:
            base = float(base)
            if base > 0 and current < base * (1 - relative):
                failures.append(f"{metric}: {current:.3f} < baseline {base:.3f} минус "
                                f"{relative:.0%}")
    return {"ok": not failures, "failures": failures, "thresholds": thresholds,
            "relative_drop": relative}


def write_baseline(report: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def format_report(report: dict, gate: dict | None = None) -> str:
    lines = ["Золотой набор: " + str((report.get("metrics") or {})), ""]
    for row in report.get("cases") or []:
        lines.append(f"{row['id']}: " + " ".join(
            f"{k}={v:.2f}" for k, v in row["metrics"].items()))
        if row.get("missing_facts"):
            lines.append("  не хватает: " + ", ".join(row["missing_facts"]))
    if gate:
        lines += ["", "Гейт: " + ("пройден" if gate["ok"] else "ПРОВАЛ"),
                  *["  - " + x for x in gate["failures"]]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Золотой набор и гейт качества")
    ap.add_argument("--golden", default=str(GOLDEN))
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--gate", action="store_true", help="вернуть 1 при провале порогов")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ragas", action="store_true", help="показать доступность RAGAS")
    ap.add_argument("--reference", action="store_true",
                    help="считать метрики на эталоне, а не через answer_core")
    a = ap.parse_args()
    try:
        golden = load_golden(a.golden)
        report = (evaluate(golden["cases"], source="reference") if a.reference
                  else evaluate(golden["cases"], answer_fn=pipeline_answer,
                                source="answer_core"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"quality_eval: {exc}", file=sys.stderr)
        return 2
    if a.ragas:
        report["ragas_requested"] = True
    if a.write_baseline:
        p = write_baseline(report, a.baseline)
        print(f"baseline записан: {p}")
    base = load_baseline(a.baseline)
    gate = check_gate(report, base) if a.gate else None
    if a.json:
        print(json.dumps({"report": report, "gate": gate}, ensure_ascii=False, indent=2))
    else:
        print(format_report(report, gate))
    return 1 if gate and not gate["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
