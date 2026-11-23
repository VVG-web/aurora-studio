"""Проверки части 265: приблизительный `timing.d` в агрегаторе «Замеров».

Дорожка B: `scripts/usage_metrics.py` читает точные записи через
`usage_log.iter_records`, приблизительные через `timing.iter_records` и держит
их раздельно. Приблизительный блок не прибавляется к точному итогу, точная
запись подавляет приблизительную того же прогона, несколько `timing.d` записей
одного прогона и вида суммируются, а `timing.jsonl` не читается.
"""
from __future__ import annotations

import importlib
import sys
import unittest.mock
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test

for _path in (str(KIT / "cockpit"), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

UM = importlib.import_module("usage_metrics")
UL = importlib.import_module("usage_log")
T = importlib.import_module("timing")


def _exact(root: Path, run: str = "run-exact", tokens_in: int = 100,
           tokens_out: int = 50, calls: int = 2) -> None:
    UL.record(str(root), run, {
        "kind": "agent", "task": "build", "calls": calls,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "by_pair": {"worker|m1": {"calls": calls, "tokens_in": tokens_in,
                                   "tokens_out": tokens_out, "seconds": 5}},
    })


def _approx(root: Path, run: str = "run-approx", tokens_in: int = 7,
            tokens_out: int = 3, calls: int = 1, kind: str = "job",
            record_id: str = "a", services: dict | None = None) -> None:
    T.record(str(root), run, {"v": 1},
             services if services is not None else {
                 "llm": {"calls": calls, "tokens_in": tokens_in,
                         "tokens_out": tokens_out}},
             kind=kind, task="agent:" + run, record_id=record_id)


@test
def test_usage_metrics_exact_and_approx_are_not_summed(tmp: Path):
    """Точный итог и приблизительный блок живут отдельно."""
    root = make_project(tmp)
    _exact(root, "run-exact", 100, 50, 2)
    _approx(root, "run-approx", 7, 3, 1)
    got = UM.collect([str(root)], days=7)
    totals, approx = got["totals"], got["approx"]
    assert totals["source"] == "usage.d" and totals["approximate"] is False, totals
    assert totals["runs"] == 1 and totals["tokens_total"] == 150, totals
    assert approx["source"] == "timing.d" and approx["approximate"] is True, approx
    assert approx["runs"] == 1 and approx["tokens_total"] == 10, approx
    assert totals["tokens_total"] != 160, totals
    assert got["quality"]["sources"] == ["usage.d", "timing.d"], got["quality"]


@test
def test_usage_metrics_exact_suppresses_approx_for_same_run_only(tmp: Path):
    """Точная запись подавляет приблизительную того же прогона, соседний остаётся."""
    root = make_project(tmp)
    _exact(root, "run-1", 100, 50, 2)
    _approx(root, "run-1", 999, 999, 9)
    _approx(root, "run-2", 7, 3, 1)
    got = UM.collect([str(root)], days=7)
    assert got["totals"]["runs"] == 1 and got["totals"]["tokens_total"] == 150, got["totals"]
    assert got["approx"]["runs"] == 1 and got["approx"]["tokens_total"] == 10, got["approx"]
    assert got["quality"]["approx_suppressed_by_exact"] == 1, got["quality"]
    assert [row["tokens_total"] for row in got["approx_by_project"]] == [10], \
        got["approx_by_project"]


@test
def test_usage_metrics_approx_row_has_source_and_null_dimensions(tmp: Path):
    """Приблизительная строка помечена и не выдумывает провайдера, модель или роль."""
    root = make_project(tmp)
    _approx(root, "run-1", 5, 2, 1)
    got = UM.collect([str(root)], days=7)
    row = got["approx_by_project"][0]
    assert row["source"] == "timing.d" and row["approximate"] is True, row
    assert row["provider"] is None and row["model"] is None and row["role"] is None, row
    assert row["tokens_total"] == 7, row
    assert row["project_id"] == "Test", row
    quality = got["quality"]
    assert quality["approx_records"] == 1 and quality["approx_tokens"] == 7, quality
    assert quality["sources"] == ["timing.d"], quality


@test
def test_usage_metrics_sums_timing_records_by_run_and_kind(tmp: Path):
    """Несколько `timing.d` одного прогона и вида дают одну сумму, разные виды отдельно."""
    root = make_project(tmp)
    _approx(root, "run-1", 10, 1, 1, kind="job", record_id="a")
    _approx(root, "run-1", 20, 2, 2, kind="job", record_id="b")
    _approx(root, "run-1", 40, 4, 4, kind="agent", record_id="c")
    got = UM.collect([str(root)], days=7)
    assert got["approx"]["runs"] == 2, got["approx"]
    assert got["approx"]["tokens_in"] == 70 and got["approx"]["tokens_out"] == 7, \
        got["approx"]
    assert got["approx"]["calls"] == 7, got["approx"]
    assert got["quality"]["approx_records"] == 2, got["quality"]
    assert len(got["approx_by_project"]) == 1, got["approx_by_project"]
    assert got["approx_by_project"][0]["runs"] == 2, got["approx_by_project"]


@test
def test_usage_metrics_ignores_embeddings_ocr_and_web(tmp: Path):
    """Из `timing.d` берётся только `services.llm`, остальные сервисы не входят."""
    root = make_project(tmp)
    _approx(root, "run-1", 10, 5, 1, services={
        "llm": {"calls": 1, "tokens_in": 10, "tokens_out": 5},
        "embeddings": {"calls": 2, "tokens_in": 999, "tokens_out": 888},
        "ocr": {"calls": 1, "tokens_in": 777, "tokens_out": 666},
        "web": {"calls": 1, "tokens_in": 555, "tokens_out": 444},
    })
    got = UM.collect([str(root)], days=7)
    assert got["approx"]["tokens_in"] == 10 and got["approx"]["tokens_out"] == 5, \
        got["approx"]
    assert got["approx"]["tokens_total"] == 15, got["approx"]
    assert got["quality"]["approx_tokens"] == 15, got["quality"]


@test
def test_usage_metrics_does_not_read_timing_jsonl(tmp: Path):
    """`timing.jsonl` не читается: даже с большими токенами он не меняет итог."""
    root = make_project(tmp)
    run_dir = root / ".aurora" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "timing.jsonl").write_text(
        '{"kind":"call","service":"llm","run_id":"run-1","metrics":'
        '{"tokens_in":999999,"tokens_out":888888}}\n', encoding="utf-8")
    _approx(root, "run-1", 3, 4, 1)
    with unittest.mock.patch.object(T, "read_spans",
                                    side_effect=AssertionError("timing.jsonl прочитан")):
        got = UM.collect([str(root)], days=7)
    assert got["approx"]["tokens_total"] == 7, got["approx"]
    assert got["quality"]["approx_tokens"] == 7, got["quality"]


@test
def test_usage_metrics_without_usage_d_returns_approx_not_error(tmp: Path):
    """Нет `usage.d`, есть `timing.d`: это приблизительный блок, а не ошибка."""
    root = make_project(tmp)
    _approx(root, "run-1", 10, 5, 2)
    got = UM.collect([str(root)], days=7)
    assert got["totals"]["runs"] == 0 and got["totals"]["tokens_total"] == 0, got["totals"]
    assert got["approx"]["runs"] == 1 and got["approx"]["tokens_total"] == 15, got["approx"]
    quality = got["quality"]
    assert quality["empty"] is False and quality["error"] == "", quality
    assert quality["sources"] == ["timing.d"], quality


@test
def test_usage_metrics_empty_project_is_empty_not_error(tmp: Path):
    """Пустой проект: честная пустота с причиной, без исключения."""
    root = make_project(tmp)
    got = UM.collect([str(root)], days=7)
    assert got["totals"]["runs"] == 0 and got["approx"]["runs"] == 0, got
    quality = got["quality"]
    assert quality["empty"] is True and quality["empty_reason"] == "no_records", quality
    assert quality["error"] == "" and quality["sources"] == [], quality
