#!/usr/bin/env python3
"""part_215 - needle-in-a-haystack MVP.

Проверяем, что контекстный путь с оглавлением и сжатием сохраняет иглы,
факты и источники. Это детерминированный тест слоя контекста, не живая модель.
"""
from __future__ import annotations

import sys

from harness import KIT, test

sys.path.insert(0, str(KIT / "scripts"))
import context_quality as Q  # noqa: E402


@test
def test_needle_suite_keeps_all_needles_under_compression(_t):
    result = Q.run_suite(cards=20, repeats=1, window=1200,
                         thresholds=(0.30, 0.70), target_ratios=(0.35, 0.50),
                         placements=("start", "spread"))
    rows = result["rows"]
    assert len(rows) == 4 * 2 * 2 * 2, len(rows)
    for row in rows:
        assert row["recall"] == 1.0, row
        assert row["groundedness"] == 1.0, row
        assert row["completeness"] == 1.0, row
        assert row["p50_ms"] >= 0 and row["p95_ms"] >= row["p50_ms"], row
        assert row["found"], row
    assert any(row["compressed"] for row in rows), "сжатие не сработало ни в одном сценарии"
    assert result["best"], result["best"]
    assert result["best"]["recall"] == 1.0


@test
def test_needle_suite_without_compression_and_with_large_window(_t):
    result = Q.run_suite(cards=12, repeats=1, window=100000,
                         thresholds=(0.30,), target_ratios=(0.50,),
                         placements=("middle", "end"))
    assert len(result["rows"]) == 4 * 1 * 1 * 2, len(result["rows"])
    for row in result["rows"]:
        assert row["compressed"] is False, row
        assert row["recall"] == 1.0 and row["completeness"] == 1.0, row


@test
def test_needle_positions_and_counts(_t):
    for count in (1, 2, 3, 4):
        pack, needles = Q.build_haystack(cards=24, needle_count=count, placement="spread")
        assert len(needles) == count
        for needle in needles:
            assert f"## {needle['id']}" in pack, (needle, pack[:200])
            assert needle["text"] in pack, needle
    start, start_needles = Q.build_haystack(cards=12, needle_count=2, placement="start")
    assert start.index("## " + start_needles[0]["id"]) < start.index("## " + start_needles[1]["id"])
    end, end_needles = Q.build_haystack(cards=12, needle_count=2, placement="end")
    assert end.index("## " + end_needles[0]["id"]) > end.index("## Заявка-0000")
