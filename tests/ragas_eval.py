#!/usr/bin/env python3
"""ragas_eval.py - расширенная RAGAS-проверка «Спросить» и «Продуктивности».

Запускать на реальном проекте при изменениях поиска, пака, ответа или артефакта:
  RAGAS_LLM_MODEL=... RAGAS_LLM_BASE_URL=... \
  RAGAS_EMBED_MODEL=... RAGAS_EMBED_BASE_URL=... \
  .venv-ragas/bin/python tests/ragas_eval.py --project /path/to/project --mode both

--project обязателен: тест проверяет реальный поиск по базе знаний, а не только
статический golden-пак. Модели и адреса задаются переменными окружения, внутренних
адресов в файле нет. RAGAS не входит в обычный CI: LLM-судья и эмбеддинги требуют модели.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
GOLDEN = KIT / "scripts" / "quality_golden.json"
sys.path.insert(0, str(KIT / "scripts"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def contexts(pack: str) -> list[str]:
    parts = re.split(r"(?m)^##\s+", str(pack or ""))
    return ["## " + p.strip() for p in parts[1:] if p.strip()] or [str(pack or "")]


def project_golden(project: str) -> list[dict]:
    """Вопросы из AuroraKnowledgeDB/meta/golden_questions.md проекта, если они есть."""
    path = Path(project) / "AuroraKnowledgeDB" / "meta" / "golden_questions.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        question, reference = cells[1], cells[2]
        if question and reference:
            out.append({"id": f"G-{cells[0]}", "question": question, "reference": reference})
    return out


def load_cases(golden: str, project: str) -> list[dict]:
    if golden:
        return json.loads(Path(golden).read_text(encoding="utf-8"))["cases"]
    cases = project_golden(project) if project else []
    if cases:
        return cases
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


def _retrieve(project: str, question: str, mode: str, max_cards: int, chars: int) -> list[str]:
    import answer_core as AC
    found = AC.search_context(project, question, mode=mode, max_cards=max_cards)
    if not found.get("ok"):
        raise RuntimeError(f"поиск не дал пак: {found.get('why') or found.get('error')}")
    return [c[:chars] for c in contexts(found.get("pack") or "")]


def _dataset(cases, project: str, mode: str, max_cards: int = 12, chars: int = 3000) -> dict:
    return {
        "question": [c["question"] for c in cases],
        "answer": [c["reference"] for c in cases],
        "contexts": [_retrieve(project, c["question"], mode, max_cards, chars)
                     if project else [x[:chars] for x in contexts(c["pack"])]
                     for c in cases],
        "ground_truth": [c["reference"] for c in cases],
    }


def _means(rows, metrics) -> dict:
    out = {}
    for metric in metrics:
        key = metric.name
        vals = [float(r[key]) for r in rows
                if r.get(key) is not None and not (isinstance(r[key], float) and math.isnan(r[key]))]
        out[key] = round(sum(vals) / len(vals), 4) if vals else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Расширенный RAGAS-прогон Code")
    ap.add_argument("--golden", default="",
                    help="JSON золотого набора; пусто — эталоны проекта или kit")
    ap.add_argument("--project", default=os.environ.get("AURORA_RAGAS_PROJECT", ""))
    ap.add_argument("--mode", choices=("ask", "product", "both"), default="both")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-cards", type=int, default=8,
                    help="сколько карточек брать из проекта (по умолчанию 8)")
    ap.add_argument("--context-chars", type=int, default=2000,
                    help="предел символов одной карточки в RAGAS (по умолчанию 2000)")
    ap.add_argument("--out", default="")
    ap.add_argument("--changed", action="store_true",
                    help="сначала проверить, нужен ли RAGAS по изменённым файлам")
    ap.add_argument("--base", default="origin/master")
    a = ap.parse_args()

    if a.changed:
        import ragas_impact
        paths = ragas_impact.changed_paths(a.base)
        verdict = ragas_impact.classify(paths)
        if not verdict["required"]:
            print("RAGAS не требуется: изменения не затрагивают получение знаний "
                  "или являются чистым дизайном.")
            return 0
        print("RAGAS обязателен: " + ", ".join(verdict["reasons"]))

    if not a.project:
        print("Нужен --project или AURORA_RAGAS_PROJECT: расширенный тест проверяет "
              "реальный поиск по базе знаний, а не только статический golden-пак.",
              file=sys.stderr)
        return 2
    if not Path(a.project).is_dir():
        print(f"Проект не найден: {a.project}", file=sys.stderr)
        return 2

    required = ("RAGAS_LLM_MODEL", "RAGAS_LLM_BASE_URL", "RAGAS_EMBED_MODEL", "RAGAS_EMBED_BASE_URL")
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        print("Не заданы переменные RAGAS: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        from datasets import Dataset
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas import evaluate
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import (
            Faithfulness,
            LLMContextRecall,
            ResponseRelevancy,
        )
        from ragas.run_config import RunConfig
    except ImportError as exc:
        print(f"RAGAS не установлен: {exc}. Нужен отдельный venv с tests/requirements-ragas.txt.",
              file=sys.stderr)
        return 2

    cases = load_cases(a.golden, a.project)
    if a.limit:
        cases = cases[:a.limit]
    modes = [("ask", "ask"), ("product", "generate")] if a.mode == "both" else [
        (a.mode, "ask" if a.mode == "ask" else "generate")]
    llm = LangchainLLMWrapper(ChatOpenAI(
        model=os.environ["RAGAS_LLM_MODEL"], base_url=os.environ["RAGAS_LLM_BASE_URL"],
        api_key=os.environ.get("RAGAS_LLM_API_KEY", "local"), temperature=0, timeout=300))
    emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(
        model=os.environ["RAGAS_EMBED_MODEL"], base_url=os.environ["RAGAS_EMBED_BASE_URL"],
        api_key=os.environ.get("RAGAS_EMBED_API_KEY", "local"),
        check_embedding_ctx_length=False, timeout=300))
    metrics = [
        Faithfulness(),
        ResponseRelevancy(strictness=1),
        LLMContextRecall(),
    ]
    out = {"project": a.project, "cases": len(cases), "modes": {}}
    for label, mode in modes:
        data = _dataset(cases, a.project, mode, max(1, a.max_cards), max(500, a.context_chars))
        result = evaluate(
            Dataset.from_dict(data), metrics=metrics, llm=llm, embeddings=emb,
            raise_exceptions=False, show_progress=False, batch_size=1,
            run_config=RunConfig(timeout=300, max_workers=1, max_retries=1),
        )
        rows = result.scores
        out["modes"][label] = {"rows": rows, "means": _means(rows, metrics)}
        if any(any(isinstance(v, float) and math.isnan(v) for v in row.values()) for row in rows):
            print("RAGAS: часть метрик не посчитана (NaN), прогон не засчитан", file=sys.stderr)
            return 1
    text = json.dumps(out, ensure_ascii=False, indent=2, default=float)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
