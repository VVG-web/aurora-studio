#!/usr/bin/env python3
"""part_221 - золотой набор, RAGAS-метрики и регрессионный гейт.

Проверяем измеримость, а не красоту текста: эталон проходит пороги, испорченный ответ
ловится, относительное падение от baseline тоже. Живая модель и сеть не нужны.
"""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from harness import KIT, test

sys.path.insert(0, str(KIT / "scripts"))
import quality_eval as Q  # noqa: E402


@test
def test_golden_set_is_measurable_and_passes_absolute_thresholds(_t):
    golden = Q.load_golden(KIT / "scripts/quality_golden.json")
    assert len(golden["cases"]) >= 5
    for case in golden["cases"]:
        assert case.get("question") and case.get("reference") and case.get("facts"), case
        assert Q.context_recall(case["facts"], case["pack"]) == 1.0, case["id"]
    report = Q.evaluate(golden["cases"])
    gate = Q.check_gate(report)
    assert gate["ok"], gate
    assert report["metrics"]["faithfulness"] >= 0.9
    assert report["metrics"]["artifact_completeness"] >= 0.8
    assert report["ragas"]["installed"] is False or report["ragas"]["installed"] is True


@test
def test_gate_catches_a_degraded_answer_and_a_relative_drop(_t):
    golden = Q.load_golden(KIT / "scripts/quality_golden.json")
    bad = Q.evaluate(golden["cases"], answer_fn=lambda c: {
        "answer": "Не знаю.", "context": c["pack"], "cards": c["cards"]})
    gate = Q.check_gate(bad)
    assert not gate["ok"]
    assert any("faithfulness" in x for x in gate["failures"]), gate

    good = Q.evaluate(golden["cases"])
    baseline = {"metrics": dict(good["metrics"], faithfulness=1.0)}
    current = {"metrics": dict(good["metrics"], faithfulness=0.94)}
    rel = Q.check_gate(current, baseline)
    assert not rel["ok"] and any("baseline" in x for x in rel["failures"]), rel


@test
def test_baseline_file_keeps_version_and_commit_and_gate_reads_it(tmp: Path):
    golden = Q.load_golden(KIT / "scripts/quality_golden.json")
    report = Q.evaluate(golden["cases"])
    path = Q.write_baseline(report, Path(tmp) / "QUALITY_BASELINE.json")
    saved = Q.load_baseline(path)
    assert saved and saved["version"] and saved["commit"]
    assert Q.check_gate(report, saved)["ok"]


@test
def test_gate_catches_lost_context_and_incomplete_artifact(_t):
    golden = Q.load_golden(KIT / "scripts/quality_golden.json")
    case = golden["cases"][0]
    lost = Q.evaluate([case], answer_fn=lambda c: {
        "answer": c["reference"], "context": "", "cards": []})
    assert lost["metrics"]["context_recall"] == 0.0
    assert lost["metrics"]["context_precision"] == 0.0
    assert not Q.check_gate(lost)["ok"]

    partial = Q.evaluate([case], answer_fn=lambda c: {
        "answer": "Срок рассмотрения заявки 10 рабочих дней.", "context": c["pack"],
        "cards": c["cards"]})
    assert partial["metrics"]["artifact_completeness"] == 0.5
    assert "artifact_completeness" in " ".join(Q.check_gate(partial)["failures"])


@test
def test_metric_definitions_are_stable_and_explain_missing_facts(_t):
    case = {"question": "Какой срок?", "pack": "## К\n\nСрок десять дней.",
            "cards": ["К", "Лишняя"], "relevant_cards": ["К"],
            "reference": "Срок десять дней [[К]].", "facts": ["десять дней", "нет такого"]}
    row = Q.evaluate_case(case)
    assert row["metrics"]["context_recall"] == 0.5
    assert row["metrics"]["context_precision"] == 0.5
    assert row["missing_facts"] == ["нет такого"]
    assert Q.faithfulness("Срок десять дней [[К]].", case["pack"]) == 1.0
    assert Q.faithfulness("Придумано навсегда.", case["pack"]) == 0.0


@test
def test_quality_eval_can_run_the_answer_core_path_without_a_network(_t):
    golden = Q.load_golden(KIT / "scripts/quality_golden.json")
    report = Q.evaluate(golden["cases"], answer_fn=Q.pipeline_answer,
                        source="answer_core")
    assert report["source"] == "answer_core"
    assert report["metrics"]["faithfulness"] >= 0.9
    assert all(row["answer"] for row in report["cases"])


@test
def test_quality_gate_cli_returns_nonzero_on_a_relative_drop(tmp: Path):
    golden = Q.load_golden(KIT / "scripts/quality_golden.json")
    assert Q.evaluate(golden["cases"], answer_fn=Q.pipeline_answer)["cases"]
    baseline = {"metrics": {k: 10.0 for k in Q.METRICS}}
    path = Q.write_baseline(baseline, Path(tmp) / "high-baseline.json")
    cp = subprocess.run(
        [sys.executable, str(KIT / "scripts/quality_eval.py"), "--gate",
         "--baseline", str(path)],
        cwd=str(KIT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60)
    out = cp.stdout + cp.stderr
    assert cp.returncode == 1, out
    assert "baseline" in out and "Гейт: ПРОВАЛ" in out, out
