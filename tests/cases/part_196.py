#!/usr/bin/env python3
"""part_196 — merge manifest.json по ключам источников (1.208.0)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness import SCRIPTS, run, test

sys.path.insert(0, str(SCRIPTS))
import install_aurora as IA  # noqa: E402


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
def test_manifest_merge_unions_sources_and_cards(tmp: Path):
    """Два участника разобрали разные источники — merge сохраняет оба и карточки."""
    base = {"version": 1, "sources": {"a.md": {"hash": "a", "processed": "2026-01-01"}},
            "cards": []}
    ours = {"version": 1, "sources": {**base["sources"],
                                      "b.md": {"hash": "b", "processed": "2026-01-02"}},
            "cards": [{"id": "b"}]}
    theirs = {"version": 1, "sources": {**base["sources"],
                                        "c.md": {"hash": "c", "processed": "2026-01-03"}},
              "cards": [{"id": "c"}]}
    merged = _merge(tmp, base, ours, theirs)
    assert set(merged["sources"]) == {"a.md", "b.md", "c.md"}, merged["sources"]
    assert {c["id"] for c in merged["cards"]} == {"b", "c"}, merged["cards"]


@test
def test_manifest_merge_prefers_newer_source_entry(tmp: Path):
    """Один и тот же источник меняли оба — побеждает более поздний processed."""
    base = {"sources": {"x.md": {"hash": "old", "processed": "2026-01-01"}}}
    ours = {"sources": {"x.md": {"hash": "ours", "processed": "2026-01-02"}}}
    theirs = {"sources": {"x.md": {"hash": "theirs", "processed": "2026-01-03"}}}
    assert _merge(tmp, base, ours, theirs)["sources"]["x.md"]["hash"] == "theirs"

    ours2 = {"sources": {"x.md": {"hash": "ours", "processed": "2026-01-04"}}}
    theirs2 = {"sources": {"x.md": {"hash": "theirs", "processed": "2026-01-03"}}}
    assert _merge(tmp, base, ours2, theirs2)["sources"]["x.md"]["hash"] == "ours"


@test
def test_manifest_merge_honors_deletion_when_other_side_unchanged(tmp: Path):
    """Удаление источника одной стороной сохраняется, если вторая не меняла запись."""
    base = {"sources": {"x.md": {"hash": "x", "processed": "2026-01-01"}}}
    ours = {"sources": {}}
    theirs = {"sources": {"x.md": {"hash": "x", "processed": "2026-01-01"}}}
    assert "x.md" not in _merge(tmp, base, ours, theirs)["sources"]

    ours2 = {"sources": {"x.md": {"hash": "x", "processed": "2026-01-01"}}}
    theirs2 = {"sources": {}}
    assert "x.md" not in _merge(tmp, base, ours2, theirs2)["sources"]


@test
def test_manifest_merge_keeps_cards_union(tmp: Path):
    """Карточки обеих сторон не теряются, порядок детерминирован."""
    base = {"cards": [{"id": "a"}]}
    ours = {"cards": [{"id": "a"}, {"id": "b"}]}
    theirs = {"cards": [{"id": "a"}, {"id": "c"}]}
    merged = _merge(tmp, base, ours, theirs)
    assert [c["id"] for c in merged["cards"]] == ["a", "b", "c"], merged["cards"]


@test
def test_gitattributes_has_manifest_merge_rule(_t):
    """Правило merge=aurora-manifest едет в проекты вместе с .gitattributes."""
    assert "AuroraKnowledgeDB/meta/manifest.json merge=aurora-manifest" in IA.GITATTRIBUTES_MERGE_BLOCK


@test
def test_install_configures_manifest_merge_driver(tmp: Path):
    """Установка настраивает git driver и повтор не меняет значение."""
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert IA.configure_merge_driver(repo) is True
    first = subprocess.run(["git", "-C", str(repo), "config", "--get",
                            "merge.aurora-manifest.driver"],
                           capture_output=True, text=True, encoding="utf-8").stdout.strip()
    assert "merge_manifest.py" in first, first
    assert IA.configure_merge_driver(repo) is True
    second = subprocess.run(["git", "-C", str(repo), "config", "--get",
                             "merge.aurora-manifest.driver"],
                            capture_output=True, text=True, encoding="utf-8").stdout.strip()
    assert second == first
