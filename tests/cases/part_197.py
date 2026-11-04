#!/usr/bin/env python3
"""part_197 — документация и версия для per-user state (1.208.1)."""
from __future__ import annotations

from harness import KIT, test


@test
def test_git_collaboration_docs_cover_local_shard_shared(_t):
    """RU/EN документация называет LOCAL, SHARD, SHARED, manifest merge и TS-013."""
    ru = (KIT / "docs" / "git-collaboration.md").read_text(encoding="utf-8")
    en = (KIT / "docs" / "en" / "git-collaboration.md").read_text(encoding="utf-8")
    for text in (ru, en):
        for term in ("LOCAL", "SHARD", "SHARED", "manifest.json",
                     "merge=aurora-manifest", "TS-013"):
            assert term in text, f"в документации нет {term}: {text[:200]}"


@test
def test_version_is_semver_for_release(_t):
    """VERSION — три числовых сегмента; релизный план ссылается на текущую версию."""
    version = (KIT / "VERSION").read_text(encoding="utf-8").strip()
    parts = version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), version
    plan = (KIT / "RELEASE_PLAN_CURRENT.md").read_text(encoding="utf-8")
    assert version in plan, f"VERSION {version} не упомянут в RELEASE_PLAN_CURRENT.md"
