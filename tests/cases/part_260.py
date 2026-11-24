"""Проверки движка Aurora, часть 260: жизненный цикл артефакта (B3).

Стадии `created -> frozen -> released`, записи `frozen[]`/`released_versions[]`,
строгий порядок «заморозка -> экспорт из снимка -> запись выпуска», читатели
`Deliverables/<kind>` и старые `Deliverables/released`. Сеть не используется,
pandoc подменён, проекты — во временных папках.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import aurora_common as C  # noqa: E402
import ship_doc as S  # noqa: E402

MARK = "<!-- ниже — производство, в чистовик не идёт -->"
BODY = "# История\n\nПлатёж проводится за пять дней.\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=str(root), check=True, capture_output=True)


def _project(tmp: Path, body: str = BODY) -> tuple:
    root = make_project(tmp, git=True)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nartifacts:\n  us:\n    title: \"История\"\n    out: Artifacts/us\n",
                   encoding="utf-8")
    doc = root / "Artifacts" / "us" / "Activity_A1" / "US-1.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text('---\ntitle: "US-1"\ndoc: US-1\nversion: "1.0"\ntype: us\nstatus: ready\n'
                   'artifact_id: ART-0001\nbased_on: ["[[REQ-001]]"]\n---\n\n' + body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "art")
    return root, "Artifacts/us/Activity_A1/US-1.md"


@contextlib.contextmanager
def _pandoc():
    """pandoc пишет то, что получил на вход, — тогда видно, из какого файла собран docx."""
    real_find, real_run = S.find_bin, S.subprocess.run

    def fake_run(cmd, *a, **kw):
        exe = str(cmd[0]) if isinstance(cmd, (list, tuple)) and cmd else ""
        if "pandoc" in exe:
            if "--version" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "pandoc 3.13\n", "")
            target = Path(cmd[cmd.index("-o") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(kw.get("input") or "docx", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *a, **kw)

    S.find_bin = lambda name: "/fake/pandoc" if name == "pandoc" else real_find(name)
    S.subprocess.run = fake_run
    try:
        yield
    finally:
        S.find_bin, S.subprocess.run = real_find, real_run


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            p = Path(dirpath) / name
            h.update(str(p.relative_to(root)).encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


def _git_status(root: Path) -> str:
    cp = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    return cp.stdout


@test
def test_lifecycle_created_frozen_released(tmp: Path):
    root, doc = _project(tmp)
    st = S.lifecycle_model(str(root), doc)
    assert st["stage"] == "created" and st["current"] == "frozen" and st["next_version"] == "1.0", st
    with _pandoc():
        fr = S.freeze(str(root), doc, "1.0", "2026-10-19", True)
        assert fr["ok"] and fr["path"].startswith("Deliverables/us/"), fr
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "freeze")
        rel = S.release(str(root), doc, "1.0", "2026-10-19", "", True)
        assert rel["ok"], rel
    st = S.lifecycle_model(str(root), doc)
    assert st["stage"] == "released" and st["current"] == "" and st["next_version"] == "1.1", st
    assert st["frozen"][0]["path"].startswith("Deliverables/us/"), st["frozen"]
    assert st["released_versions"][0]["frozen_sha256"] == st["frozen"][0]["sha256"], st
    assert "Deliverables/released" not in st["frozen"][0]["path"], st["frozen"]


@test
def test_publication_does_not_change_stage(tmp: Path):
    root, doc = _project(tmp)
    path = root / doc
    text = path.read_text(encoding="utf-8")
    text = text.replace("---\n\n", 'confluence_page_id: "884422"\npublished: 2026-10-20\n'
                                    'published_url: https://wiki.example.com/pages/viewpage.action?pageId=884422\n---\n\n')
    text = text.replace("published_url:", "published_sha: " + C.clean_sha(text) + "\npublished_url:")
    path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "published")
    st = S.lifecycle_model(str(root), doc)
    assert st["stage"] == "created" and st["problems"] == [], st
    assert st["publication"]["state"] == "published" and st["publication"]["done"] is True, st
    assert st["publication"]["stale"] is False and st["required_for"] == {"freeze": ["created"],
                                                                         "release": ["created", "frozen"]}, st


@test
def test_freeze_is_idempotent_and_rejects_changed_text(tmp: Path):
    root, doc = _project(tmp)
    with _pandoc():
        first = S.freeze(str(root), doc, "1.0", "2026-10-19", True)
        again = S.freeze(str(root), doc, "1.0", "2026-10-19", True)
    assert first["ok"] and again["ok"] and again.get("unchanged"), (first, again)
    snaps = sorted((root / "Deliverables" / "us").rglob("*.md"))
    assert len(snaps) == 1, snaps
    path = root / doc
    path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
    with _pandoc():
        bad = S.freeze(str(root), doc, "1.0", "2026-10-19", True, allow_dirty=True)
    assert not bad["ok"] and bad["code"] == "version-taken", bad
    assert len(sorted((root / "Deliverables" / "us").rglob("*.md"))) == 1


@test
def test_release_reads_snapshot_not_source(tmp: Path):
    root, doc = _project(tmp)
    with _pandoc():
        assert S.freeze(str(root), doc, "1.0", "2026-10-19", True)["ok"]
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "freeze")
        path = root / doc
        path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "сто дней"), encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "source changed")
        rel = S.release(str(root), doc, "1.0", "2026-10-19", "", True)
    assert rel["ok"], rel
    docx = (root / rel["release"]["path"]).read_text(encoding="utf-8")
    assert "пять дней" in docx and "сто дней" not in docx, docx
    assert rel["release"]["frozen_path"].startswith("Deliverables/us/"), rel["release"]


@test
def test_release_order_and_partial_success(tmp: Path):
    root, doc = _project(tmp)
    with _pandoc():
        assert S.freeze(str(root), doc, "1.0", "2026-10-19", True)["ok"]
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "freeze")
    real_export = S.export
    S.export = lambda *a, **kw: 1
    try:
        bad = S.release(str(root), doc, "1.0", "2026-10-19", "", True)
    finally:
        S.export = real_export
    assert not bad["ok"], bad
    st = S.lifecycle_model(str(root), doc)
    assert len(st["frozen"]) == 1 and st["released_versions"] == [] and st["stage"] == "frozen", st
    assert list((root / "Deliverables" / "us").rglob("*.md")), "снимок не должен исчезнуть"
    # сбой записи выпуска: docx и снимок остаются, записи нет
    real_with = S.with_records
    def broken(text, field, records):
        if field == "released_versions":
            raise OSError("диск полон")
        return real_with(text, field, records)
    S.with_records = broken
    with _pandoc():
        try:
            bad2 = S.release(str(root), doc, "1.0", "2026-10-19", "", True)
        finally:
            S.with_records = real_with
    assert not bad2["ok"] and bad2["code"] == "record-failed", bad2
    st = S.lifecycle_model(str(root), doc)
    assert st["released_versions"] == [] and st["stage"] == "frozen", st
    assert list((root / "Deliverables" / "us").rglob("*.docx")), "docx не должен исчезнуть"


@test
def test_legacy_released_scalar_is_read(tmp: Path):
    root, doc = _project(tmp)
    path = root / doc
    path.write_text(path.read_text(encoding="utf-8").replace("status: ready\n", "status: ready\nreleased: 2026-05-05\n"),
                    encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "legacy")
    st = S.lifecycle_model(str(root), doc)
    assert st["stage"] == "released" and st["current"] == "", st
    assert st["released_versions"] and st["released_versions"][0]["legacy"] is True, st


@test
def test_snapshot_path_survives_windows_and_macos_case(_t):
    import id_registry as I
    kinds = {"us": {}, "ac": {}}
    assert I.snapshot_path("Deliverables/us/A/US-1_v1.md", kinds)
    assert I.snapshot_path("deliverables/US/A/US-1_v1.md", kinds)
    assert I.snapshot_path("Deliverables\\us\\A\\US-1_v1.md", kinds)
    assert not I.snapshot_path("Deliverables/work/A/US-1.md", kinds)


@test
def test_snapshot_path_rejects_work2_and_unknown(_t):
    import id_registry as I
    kinds = {"us": {}}
    assert not I.snapshot_path("Deliverables/work2/x.md", kinds)
    assert not I.snapshot_path("Deliverables/unknown/x.md", kinds)
    assert I.snapshot_path("Deliverables/us/x.md", kinds)


@test
def test_snapshot_path_accepts_legacy_deliverable(_t):
    import id_registry as I
    assert I.snapshot_path("Deliverables/deliverable/x.md", {"us": {}})
    assert not I.snapshot_path("Deliverables/work2/x.md", {"us": {}})
    assert not I.snapshot_path("Deliverables/released/x.md", {"us": {}})


@test
def test_readonly_snapshot_is_evidence(tmp: Path):
    import aurora_doctor as AD
    import id_registry as I
    import kb_scrub
    root, doc = _project(tmp)
    with _pandoc():
        assert S.freeze(str(root), doc, "1.0", "2026-10-19", True)["ok"]
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "freeze")
    assert not I.duplicates(str(root), ["ART"]), I.duplicates(str(root), ["ART"])
    kinds = {"us": {}}
    assert kb_scrub.evidence_path("Deliverables/us/A/US-1_v1.0_2026-10-19.md", kinds)
    here = os.getcwd()
    os.chdir(root)
    try:
        assert any("Deliverables/us" in p for p in kb_scrub.targets(True, kinds)), kb_scrub.targets(True, kinds)
    finally:
        os.chdir(here)
    snap = next((root / "Deliverables" / "us").rglob("*.md"))
    snap.write_text(snap.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
    found = C.released_findings(str(root), with_dir=True)
    assert any(k == "hash" and a == snap.name and "Deliverables/us" in d for k, a, _b, d in found), found
    old = AD.ROOT
    AD.ROOT = root
    try:
        assert any(snap.name in line for line in AD.check_released()), AD.check_released()
    finally:
        AD.ROOT = old


@test
def test_verify_catches_hash_delete_and_extra(tmp: Path):
    root, doc = _project(tmp)
    with _pandoc():
        rel = S.release(str(root), doc, "1.0", "2026-10-19", "", True)
    assert rel["ok"], rel
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "release")
    assert S.verify(str(root))["ok"], S.verify(str(root))
    snap = root / rel["snapshot"]
    snap.write_text(snap.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
    docx = root / rel["release"]["path"]
    docx.unlink()
    extra = snap.parent / "лишний.md"
    extra.write_text("---\nsha256: x\n---\n\n# лишний\n", encoding="utf-8")
    # Старая папка не выпадает из проверки: снимок без записи — тоже лишний файл.
    legacy = root / "Deliverables" / "released"
    legacy.mkdir(parents=True, exist_ok=True)
    old = legacy / "old_v1.0_2026-05-05.md"
    old.write_text("---\ntype: deliverable\ndoc: old\nversion: \"1.0\"\n---\n\n# старый\n", encoding="utf-8")
    got = S.verify(str(root))
    codes = {item["code"] for item in got["problems"]}
    assert not got["ok"] and "hash-mismatch" in codes and "release-missing" in codes and "extra-file" in codes, got
    assert any(item["path"] == "Deliverables/released/old_v1.0_2026-05-05.md" and item["code"] == "extra-file"
               for item in got["problems"]), got


@test
def test_status_json_contract(tmp: Path):
    root, doc = _project(tmp)
    cp = run("ship_doc.py", doc, "--status", "--json", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    got = json.loads(cp.stdout)
    for key in ("applicable", "stage", "required_for", "current", "problems", "publication"):
        assert key in got, got
    assert got["applicable"] is True and got["stage"] == "created" and got["current"] == "frozen", got
    with _pandoc():
        assert S.freeze(str(root), doc, "1.0", "2026-10-19", True)["ok"]
    got2 = json.loads(run("ship_doc.py", doc, "--status", "--json", cwd=root).stdout)
    assert got2["stage"] == "frozen" and got2["current"] == "released", got2


@test
def test_freeze_dry_run_writes_nothing(tmp: Path):
    root, doc = _project(tmp)
    before, status = _tree_digest(root), _git_status(root)
    got = S.freeze(str(root), doc, "1.0", "2026-10-19", False)
    assert got["ok"] and got["dry_run"], got
    assert _tree_digest(root) == before and _git_status(root) == status, "dry-run что-то записал"


@test
def test_release_dry_run_writes_nothing(tmp: Path):
    root, doc = _project(tmp)
    path = root / doc
    path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
    before, status = _tree_digest(root), _git_status(root)
    got = S.release(str(root), doc, "1.0", "2026-10-19", "", False, allow_dirty=True)
    assert got["ok"] and got["dry_run"], got
    assert _tree_digest(root) == before and _git_status(root) == status, "dry-run что-то записал"
    assert not (root / "Deliverables").exists() or not list((root / "Deliverables").rglob("*.docx"))


@test
def test_ship_plan_tree_unchanged(tmp: Path):
    root, doc = _project(tmp)
    before, status = _tree_digest(root), _git_status(root)
    assert S.freeze(str(root), doc, "1.0", "2026-10-19", False)["ok"]
    assert S.release(str(root), doc, "1.0", "2026-10-19", "", False)["ok"]
    assert _tree_digest(root) == before and _git_status(root) == status, "план тронул дерево"
    assert not (root / ".aurora" / "state" / "lifecycle").exists(), "план создал замок"
