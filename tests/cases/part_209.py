#!/usr/bin/env python3
"""part_209 — 1.212.1: вложенные AC и корневой Code/* (C.8, C.11)."""
from __future__ import annotations

import sys
from pathlib import Path

from harness import KIT, make_project, test


@test
def test_artifact_files_sees_nested_ac_and_skips_other_kinds(tmp: Path):
    """AC в Activity/Epic виден, папка другого вида не подмешивается."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    root = make_project(tmp)
    for rel in ("Artifacts/ac/AC-1.md", "Artifacts/ac/Activity/Epic/AC-2.md",
                "Artifacts/us/US-1.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nstatus: ready\n---\n\n# Документ\n", encoding="utf-8")
    old_read, saved_publish = ck.AG_KINDS.read_kinds, ck.publish_info
    ck.AG_KINDS.read_kinds = lambda _project: {
        "ac": {"out": "Artifacts/ac"}, "us": {"out": "Artifacts/us"}}
    ck.publish_info = lambda *_a, **_k: {"state": "none"}
    try:
        got = {item["rel"] for item in ck.artifact_files(str(root), "ac")}
    finally:
        ck.AG_KINDS.read_kinds, ck.publish_info = old_read, saved_publish
    assert "Artifacts/ac/AC-1.md" in got
    assert "Artifacts/ac/Activity/Epic/AC-2.md" in got, got
    assert "Artifacts/us/US-1.md" not in got, got


@test
def test_root_gitignore_protects_code(_t):
    """Корень кита не коммитит чужой код: Code/* с исключениями для описания."""
    text = (KIT / ".gitignore").read_text(encoding="utf-8")
    assert "Code/*" in text and "!Code/.gitkeep" in text and "!Code/README.md" in text
