#!/usr/bin/env python3
"""part_201 — два клона сливают manifest.json без конфликта (1.209.2)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness import SCRIPTS, test


def _git(repo: Path, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def _manifest(path: Path, sources: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "sources": sources, "cards": []},
                               ensure_ascii=False, indent=1), encoding="utf-8")


@test
def test_two_clones_merge_manifest_without_conflict(tmp: Path):
    """Два клона правят разные источники; pull проходит через драйвер без конфликта."""
    remote = tmp / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)

    src = tmp / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "master")
    _git(src, "config", "user.name", "Owner")
    _git(src, "config", "user.email", "owner@example.com")
    (src / ".gitattributes").write_text(
        "AuroraKnowledgeDB/meta/manifest.json merge=aurora-manifest\n", encoding="utf-8")
    (src / ".aurora/scripts").mkdir(parents=True)
    (src / ".aurora/scripts/merge_manifest.py").write_text(
        (SCRIPTS / "merge_manifest.py").read_text(encoding="utf-8"), encoding="utf-8")
    _manifest(src / "AuroraKnowledgeDB/meta/manifest.json",
              {"base.md": {"hash": "b", "processed": "2026-01-01"}})
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "base")
    _git(src, "remote", "add", "origin", str(remote))
    _git(src, "push", "-qu", "origin", "master")

    clones = []
    for name in ("a", "b"):
        clone = tmp / name
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
        _git(clone, "config", "user.name", name)
        _git(clone, "config", "user.email", f"{name}@example.com")
        _git(clone, "config", "merge.aurora-manifest.name", "Aurora manifest merge")
        driver = f'"{sys.executable}" "{SCRIPTS / "merge_manifest.py"}" %O %A %B %L %P'
        _git(clone, "config", "merge.aurora-manifest.driver", driver)
        clones.append(clone)

    a, b = clones
    for clone, rel in ((a, "a.md"), (b, "b.md")):
        path = clone / "AuroraKnowledgeDB/meta/manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sources"][rel] = {"hash": rel[0], "processed": "2026-01-02"}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-qm", f"add {rel}")

    _git(a, "push", "-q", "origin", "master")
    pulled = _git(b, "pull", "--no-rebase", "origin", "master", check=False)
    assert pulled.returncode == 0, pulled.stdout + pulled.stderr
    assert "CONFLICT" not in (pulled.stdout + pulled.stderr), pulled.stdout + pulled.stderr

    merged = json.loads((b / "AuroraKnowledgeDB/meta/manifest.json").read_text(encoding="utf-8"))
    assert set(merged["sources"]) == {"base.md", "a.md", "b.md"}, merged["sources"]
    assert _git(b, "status", "--porcelain").stdout.strip() == "", "после merge дерево грязное"
