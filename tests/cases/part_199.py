#!/usr/bin/env python3
"""part_199 — manifest cards по id и классификация diagnostics (1.209.1)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, run, test

sys.path.insert(0, str(SCRIPTS))


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _merge(tmp: Path, base: dict, ours: dict, theirs: dict) -> dict:
    b, o, t = tmp / "base.json", tmp / "ours.json", tmp / "theirs.json"
    _write(b, base)
    _write(o, ours)
    _write(t, theirs)
    run("merge_manifest.py", str(b), str(o), str(t), "7", "manifest.json",
        cwd=tmp, expect_rc=0)
    return json.loads(o.read_text(encoding="utf-8"))


@test
def test_manifest_cards_merge_by_id_and_newer_metadata(tmp: Path):
    """Одна карточка с разными метаданными не дублируется: побеждает более новый updated."""
    base = {"cards": [{"id": "a", "updated": "2026-01-01", "title": "old"}]}
    ours = {"cards": [{"id": "a", "updated": "2026-01-02", "title": "ours"},
                      {"id": "b", "updated": "2026-01-02", "title": "b"}]}
    theirs = {"cards": [{"id": "a", "updated": "2026-01-03", "title": "theirs"},
                        {"id": "c", "updated": "2026-01-03", "title": "c"}]}
    cards = _merge(tmp, base, ours, theirs)["cards"]
    by_id = {c["id"]: c for c in cards}
    assert set(by_id) == {"a", "b", "c"}, cards
    assert by_id["a"]["title"] == "theirs", by_id["a"]


@test
def test_manifest_cards_merge_honors_deletion(tmp: Path):
    """Удаление карточки одной стороной сохраняется, если вторая не меняла её."""
    base = {"cards": [{"id": "a", "updated": "2026-01-01"}]}
    ours = {"cards": []}
    theirs = {"cards": [{"id": "a", "updated": "2026-01-01"}]}
    assert _merge(tmp, base, ours, theirs)["cards"] == []


@test
def test_manifest_cards_without_id_union_by_content(tmp: Path):
    """Карточки без id/name/path объединяются по содержимому, без потерь."""
    base = {"cards": []}
    ours = {"cards": [{"text": "x"}]}
    theirs = {"cards": [{"text": "y"}]}
    cards = _merge(tmp, base, ours, theirs)["cards"]
    assert {c["text"] for c in cards} == {"x", "y"}, cards


@test
def test_docs_classify_diagnostics(_t):
    """RU/EN документация называет build_failures, unsupported и trace с классами."""
    documents = [
        (KIT / "docs/git-collaboration.md").read_text(encoding="utf-8"),
        (KIT / "docs/en/git-collaboration.md").read_text(encoding="utf-8"),
    ]
    for text in documents:
        for term in ("build_failures.json", "unsupported", "trace", "SHARED", "UNSURE"):
            assert term in text, f"классификация diagnostics неполна: {term}"
