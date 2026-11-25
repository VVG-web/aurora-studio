"""Проверки движка Aurora, часть 264: миграция старых снимков и legacy-чтение.

Старые `Deliverables/released` остаются на месте; `--migrate` только дописывает
`frozen[]` с `legacy: true`. Старый скаляр `released`, `binary` и
`Deliverables/work` без вида читаются, но новых полей не пишут.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import aurora_common as C  # noqa: E402
import ship_doc as S  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=str(root), check=True, capture_output=True)


def _project(tmp: Path) -> Path:
    root = make_project(tmp, git=True)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nartifacts:\n  us:\n    title: \"История\"\n    out: Artifacts/us\n",
                   encoding="utf-8")
    return root


def _source(root: Path, rel: str, doc: str = "US-1", version: str = "1.0") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{doc}"\ndoc: {doc}\nversion: "{version}"\ntype: us\n'
                    f'status: ready\nartifact_id: ART-0001\n'
                    f'based_on: ["[[REQ-001]]"]\n---\n\n# {doc}\n\nТекст.\n', encoding="utf-8")
    return path


def _legacy(root: Path, name: str, head: str = "", body: str = "# Документ\n\nТекст.\n") -> Path:
    path = root / "Deliverables" / "released" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\ntype: deliverable\ndoc: {name.split('_v')[0]}\nversion: \"1.0\"\n{head}---\n\n{body}"
    path.write_text(text, encoding="utf-8")
    return path


@test
def test_migrate_unique_and_ambiguous(tmp: Path):
    root = _project(tmp)
    _source(root, "Artifacts/us/A/US-1.md")
    _source(root, "Artifacts/us/B/US-1.md")
    snap = _legacy(root, "US-1_v1.0_2026-05-05.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "legacy")
    got = S.migrate(str(root), apply=False)
    item = next(x for x in got["plan"] if x["snapshot"].endswith(snap.name))
    assert item["status"] == "ambiguous" and len(item["candidates"]) == 2, item
    assert not got["ok"]
    for rel in ("Artifacts/us/A/US-1.md", "Artifacts/us/B/US-1.md"):
        assert C.list_records((root / rel).read_text(encoding="utf-8"), "frozen") == []
    (root / "Artifacts/us/B/US-1.md").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one source")
    got2 = S.migrate(str(root), apply=True)
    item = next(x for x in got2["plan"] if x["snapshot"].endswith(snap.name))
    assert item["status"] == "unique" and item["source"] == "Artifacts/us/A/US-1.md", item
    recs = C.list_records((root / "Artifacts/us/A/US-1.md").read_text(encoding="utf-8"), "frozen")
    assert len(recs) == 1 and recs[0]["legacy"] is True and recs[0]["path"] == "Deliverables/released/" + snap.name, recs


@test
def test_migrate_link_explicit(tmp: Path):
    root = _project(tmp)
    _source(root, "Artifacts/us/A/US-1.md")
    _source(root, "Artifacts/us/B/US-1.md")
    snap = _legacy(root, "US-1_v1.0_2026-05-05.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "legacy")
    rel = "Deliverables/released/" + snap.name
    got = S.migrate(str(root), apply=True, links={rel: "Artifacts/us/B/US-1.md"})
    item = next(x for x in got["plan"] if x["snapshot"] == rel)
    assert item["status"] == "unique" and item["source"] == "Artifacts/us/B/US-1.md", item
    recs = C.list_records((root / "Artifacts/us/B/US-1.md").read_text(encoding="utf-8"), "frozen")
    assert len(recs) == 1 and recs[0]["legacy"] is True, recs


@test
def test_old_released_folder_is_not_moved(tmp: Path):
    root = _project(tmp)
    _source(root, "Artifacts/us/A/US-1.md")
    snap = _legacy(root, "US-1_v1.0_2026-05-05.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "legacy")
    before = snap.read_bytes()
    assert S.migrate(str(root), apply=True)["ok"], S.migrate(str(root), apply=True)
    assert snap.exists() and snap.read_bytes() == before, "старый снимок переехал или изменился"
    assert not list((root / "Deliverables" / "deliverable").glob("*")), "старый снимок скопирован в новый вид"


@test
def test_old_released_scalar_and_binary_are_read(tmp: Path):
    root = _project(tmp)
    rel = root / "Deliverables" / "released" / "Old_v1.0_2026-05-05.md"
    rel.parent.mkdir(parents=True, exist_ok=True)
    binary = rel.parent / "built.docx"
    binary.write_bytes(b"docx-bytes")
    import hashlib
    text = ("---\ntype: deliverable\ndoc: Old\nversion: \"1.0\"\nreleased: 2026-05-05\n"
            "binary: built.docx\nbinary_sha256: " + hashlib.sha256(binary.read_bytes()).hexdigest() + "\n"
            "---\n\n# Old\n\nТекст.\n")
    text = text.replace("---\n\n", f"sha256: {C.release_hash(text)}\n---\n\n", 1)
    rel.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "legacy")
    before = rel.read_text(encoding="utf-8")
    st = S.lifecycle_model(str(root), str(rel.relative_to(root)))
    assert st["applicable"] is True and st["stage"] == "released", st
    assert st["released_versions"] and st["released_versions"][0]["legacy"] is True, st
    assert any(str(r.get("path", "")).endswith("built.docx") for r in st["released_versions"]), st
    assert rel.read_text(encoding="utf-8") == before, "чтение старого снимка что-то записало"
    assert "released_versions" not in before, "чтение дописало новый список"


@test
def test_old_work_without_kind_uses_deliverable(tmp: Path):
    root = _project(tmp)
    work = root / "Deliverables" / "work" / "Old_v1.0.md"
    work.parent.mkdir(parents=True, exist_ok=True)
    work.write_text('---\ntitle: "Old"\ndoc: Old\nversion: "1.0"\ntype: deliverable\nstatus: ready\n---\n\n# Old\n\nТекст.\n',
                   encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "work")
    got = S.freeze(str(root), "Deliverables/work/Old_v1.0.md", "1.0", "2026-05-05", True)
    assert got["ok"] and got["path"] == "Deliverables/deliverable/Old_v1.0_2026-05-05.md", got
    assert not list((root / "Deliverables" / "released").glob("*.md")), "новый снимок попал в legacy-папку"


@test
def test_old_artifact_without_frozen_is_applicable(tmp: Path):
    root = _project(tmp)
    _source(root, "Artifacts/us/A/US-1.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "art")
    st = S.lifecycle_model(str(root), "Artifacts/us/A/US-1.md")
    assert st["applicable"] is True and st["stage"] == "created" and st["current"] == "frozen", st
    assert st["frozen"] == [] and st["problems"] == [], st
