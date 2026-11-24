"""Проверки части 276: выпуск Word из замороженного снимка.

Заморозка фиксирует не только текст, но и поля сборки docx; выпуск читает
снимок и конфиг вида, собирается через Word-preview (эталон, титул, checks,
TOC, логотип) и пишет неизменяемый отчёт проверок в `Artifacts/word-checks/`.
Текущий исходник при этом не читается. Проверки не ходят в сеть и не поднимают
Word: pandoc запускается на синтетическом проекте; без pandoc проверки
пропускаются с явной причиной, схема полей проверяется всегда.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

from harness import SCRIPTS, make_project, test
from docfix import NS, docx

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import make_kinds as MK  # noqa: E402
import ship_doc as S  # noqa: E402

WORD_CHECKS = Path("Artifacts/word-checks")
CHECK_RE = re.compile(r"Artifacts/word-checks/check-[0-9a-f]{64}\.json")


def _png(width: int = 1, height: int = 1, pad: int = 1200) -> bytes:
    """Настоящий PNG 1×1 с tEXt-паддингом: проходит и сигнатуру, и порог 1 КБ."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"tEXt", b"Comment\x00" + b"x" * pad)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _docx_part(path: Path, name: str = "word/document.xml") -> str:
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf-8")


def _require_pandoc() -> bool:
    if S.find_bin("pandoc"):
        return True
    print("SKIP part_276: pandoc не найден; выпуск Word не проверяется")
    return False


def _reference_docx(path: Path) -> None:
    """Эталон с каноническими стилями pandoc: Title, Heading 1..2, TOC Heading."""
    styles = (
        '<w:styles %s>'
        '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" '
        'w:hAnsi="Times New Roman"/></w:rPr></w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>'
        '<w:basedOn w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
        '<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>'
        '<w:basedOn w:val="Normal"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="TOCHeading"><w:name w:val="TOC Heading"/>'
        '<w:basedOn w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/>'
        '<w:basedOn w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="FirstParagraph">'
        '<w:name w:val="First Paragraph"/><w:basedOn w:val="BodyText"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Compact"><w:name w:val="Compact"/>'
        '<w:basedOn w:val="BodyText"/></w:style>'
        '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/></w:style>'
        '<w:style w:type="table" w:styleId="Table"><w:name w:val="Table"/></w:style>'
        '</w:styles>' % NS
    )
    docx(path, '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
               '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" w:left="1417"/></w:sectPr>',
         parts={"word/styles.xml": styles}, standard=False)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   cwd=str(root), check=True, capture_output=True)


def _project(tmp: Path, *, checks_extra: dict | None = None, logo: bool = True) -> Path:
    root = make_project(tmp, git=True)
    for folder in ("Templates", "Checks", "Artifacts/opz", "Artifacts/word-checks"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Раздел\n", encoding="utf-8")
    _reference_docx(root / "Templates/base.docx")
    title = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
             '<w:body><w:p><w:r><w:t>ТИТУЛ-276</w:t></w:r></w:p>'
             '<w:p><w:r><w:t>{{customer}}</w:t></w:r></w:p></w:body></w:document>')
    (root / "Checks/title.xml").write_text(title, encoding="utf-8")
    checks = {
        "version": 1,
        "kind": "opz",
        "required_sections": [{"level": 1, "text": "Введение"}, {"level": 2, "text": "Цель"}],
        "title_fields": {"customer": "ООО Ромашка"},
        "toc": {"enabled": True, "depth": 2, "title": "Содержание"},
        "page": {"size": "A4", "orientation": "portrait",
                 "margins_mm": {"top": 20, "right": 15, "bottom": 20, "left": 25},
                 "header_mm": 10, "footer_mm": 10},
        "fonts": {"body": "Times New Roman", "heading": "Times New Roman"},
        "images": {"enabled": True, "max_count": 50, "max_bytes": 5242880, "formats": ["png", "jpeg"]},
    }
    cfg = ('\nartifacts:\n  opz:\n    title: "ОПЗ"\n    template: Templates/AC.md\n'
           '    out: Artifacts/opz\n    docx_checks: Checks/opz.json\n'
           '    docx_template: Templates/base.docx\n    docx_title: Checks/title.xml\n'
           '    docx_engine: auto\n')
    if logo:
        logo_bytes = _png()
        (root / "Checks/logo.png").write_bytes(logo_bytes)
        checks["images"]["logo_sha256"] = hashlib.sha256(logo_bytes).hexdigest()
        cfg += "    docx_logo: Checks/logo.png\n"
    if checks_extra:
        checks.update(checks_extra)
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    with open(root / "aurora.config.yaml", "a", encoding="utf-8") as f:
        f.write(cfg)
    return root


def _source(root: Path, body: str | None = None) -> str:
    rel = "Artifacts/opz/OPZ-1.md"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ('---\ntitle: "ОПЗ-276"\ndoc: OPZ-1\nversion: "1.0"\ntype: opz\nstatus: ready\n'
            'artifact_id: ART-276\ndate: "2026-01-02"\nbased_on: ["[[REQ-001]]"]\n---\n\n'
            + (body or "# Введение\n\nПЕРВЫЙ-276.\n\n## Цель\n\nВТОРОЙ-276.\n"))
    path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "source")
    return rel


def _snapshot(root: Path, rel: str) -> tuple[str, Path]:
    got = S.freeze(str(root), rel, "1.0", "2026-01-02", True)
    assert got.get("ok"), got
    path = root / got["path"]
    assert path.is_file(), path
    return got["path"], path


def _immutable_checks(value, root: Path) -> str:
    rel = str(value or "").replace("\\", "/")
    assert CHECK_RE.fullmatch(rel), rel
    assert (root / rel).is_file(), rel
    return rel


def _records(root: Path, rel: str) -> dict:
    return S.lifecycle_model(str(root), rel)


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            path = Path(dirpath) / name
            h.update(str(path.relative_to(root)).encode("utf-8"))
            h.update(path.read_bytes())
    return h.hexdigest()


@test
def test_release_builds_docx_through_preview_pipeline(tmp: Path):
    """Выпуск идёт через preview: эталон и титул применены, отчёт проверок лежит
    в `Artifacts/word-checks/`, а не рядом с docx."""
    if not _require_pandoc():
        return
    root = _project(tmp)
    rel = _source(root)
    _snapshot(root, rel)
    got = S.release(str(root), rel, "1.0", "2026-01-02", "", True)
    assert got.get("ok"), got
    record = got["release"]
    docx = root / record["path"]
    assert docx.is_file() and docx.suffix == ".docx", record
    assert record["path"].startswith("Deliverables/opz/"), record

    document = _docx_part(docx)
    assert "ТИТУЛ-276" in document, document[:3000]
    assert "ООО Ромашка" in document, document[:3000]
    assert "ПЕРВЫЙ-276" in document and "ВТОРОЙ-276" in document, document[:3000]

    report_rel = _immutable_checks(record["checks"], root)
    assert "Deliverables" not in report_rel, report_rel
    report = json.loads((root / report_rel).read_text(encoding="utf-8"))
    assert report["blocked"] is False, report
    assert report["kind"] == "opz", report
    assert report["docx_checks"] == "Checks/opz.json", report
    assert str(report["reference"]).replace("\\", "/").endswith("Templates/base.docx"), report
    assert report["toc_depth"] == 2, report
    assert report["paths"]["check"] == report_rel, report
    assert str(report["paths"]["docx"]).replace("\\", "/").endswith(record["path"]), report
    assert not list((root / "Deliverables").rglob("check.json")), "отчёт проверок попал в Deliverables"


@test
def test_failed_checks_block_release_and_keep_snapshot(tmp: Path):
    """Провал обязательного раздела — машинный код `docx-checks-failed`, записи
    выпуска нет, снимок и запись заморозки не меняются."""
    if not _require_pandoc():
        return
    root = _project(tmp, checks_extra={"required_sections": [{"level": 1, "text": "Отсутствующий раздел"}]})
    rel = _source(root)
    snap_rel, snap = _snapshot(root, rel)
    before = snap.read_bytes()
    got = S.release(str(root), rel, "1.0", "2026-01-02", "", True)
    assert not got.get("ok"), got
    assert got.get("code") == "docx-checks-failed", got
    assert got.get("checks_status") == "failed", got
    if got.get("checks"):
        _immutable_checks(got["checks"], root)

    model = _records(root, rel)
    assert model["stage"] == "frozen", model
    assert model["released_versions"] == [], model
    assert len(model["frozen"]) == 1 and model["frozen"][0]["path"] == snap_rel, model
    assert snap.read_bytes() == before, "снимок изменился после отказа проверок"
    assert not list((root / "Deliverables/opz").glob("*.docx")), "docx выпущен при провале checks"


@test
def test_source_is_not_read_after_freeze(tmp: Path):
    """Опции и вид берутся из шапки снимка: исходник изменён, переведён в другой вид
    и удалён — docx всё равно собран из снимка с прежними checks, эталоном и титулом."""
    if not _require_pandoc():
        return
    root = _project(tmp)
    rel = _source(root)
    snap_rel, snap = _snapshot(root, rel)
    head = snap.read_text(encoding="utf-8")
    assert "kind: opz" in head and "docx_checks: Checks/opz.json" in head, head[:2000]

    source = root / rel
    source.write_text(source.read_text(encoding="utf-8").replace("type: opz", "type: us")
                      .replace("ПЕРВЫЙ-276", "СТО-276"), encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   .replace("docx_checks: Checks/opz.json", "docx_checks: Checks/missing.json")
                   .replace("docx_template: Templates/base.docx", "docx_template: Templates/missing.docx")
                   .replace("docx_title: Checks/title.xml", "docx_title: Checks/missing.xml"),
                   encoding="utf-8")
    released = S.release(str(root), rel, "1.0", "2026-01-02", "", True)
    assert released.get("ok"), released
    document = _docx_part(root / released["release"]["path"])
    assert "ПЕРВЫЙ-276" in document and "СТО-276" not in document, document[:3000]
    assert "ТИТУЛ-276" in document and "ООО Ромашка" in document, document[:3000]

    source.unlink()
    assert not source.exists()
    out = "Deliverables/opz/from-snapshot.docx"
    got = S.export_frozen(str(root), rel, "1.0", out=out, quiet=True)
    assert got.get("ok"), got
    docx = root / out
    assert docx.is_file(), got
    document = _docx_part(docx)
    assert "ПЕРВЫЙ-276" in document and "СТО-276" not in document, document[:3000]
    assert "ТИТУЛ-276" in document and "ООО Ромашка" in document, document[:3000]

    report_rel = _immutable_checks(got["checks"], root)
    report = json.loads((root / report_rel).read_text(encoding="utf-8"))
    assert report["docx_checks"] == "Checks/opz.json", report
    assert report["kind"] == "opz", report
    assert str(report["reference"]).replace("\\", "/").endswith("Templates/base.docx"), report
    assert not source.exists(), "выпуск создал исходник"


@test
def test_release_record_names_checks_and_template_evidence(tmp: Path):
    """`released_versions[]` хранит checks, checks_sha256, template*, frozen* и tool*;
    план не появляется, если W5-план не использовался."""
    if not _require_pandoc():
        return
    root = _project(tmp)
    rel = _source(root)
    snap_rel, _snap = _snapshot(root, rel)
    got = S.release(str(root), rel, "1.0", "2026-01-02", "", True)
    assert got.get("ok"), got
    record = got["release"]
    for field in ("checks", "checks_sha256", "template", "template_version", "template_sha256",
                  "frozen_sha256", "frozen_path", "tool", "tool_version"):
        assert field in record, (field, record)
    report_rel = _immutable_checks(record["checks"], root)
    assert record["checks_sha256"] == _sha(root / report_rel), record
    assert record["template"] == "Templates/base.docx", record
    assert record["template_sha256"] == _sha(root / "Templates/base.docx"), record
    assert isinstance(record["template_version"], str), record
    assert record["frozen_path"] == snap_rel, record
    assert record["frozen_sha256"] == _records(root, rel)["frozen"][0]["sha256"], record
    assert record["tool"] == "pandoc" and record["tool_version"], record
    assert "plan" not in record and "plan_sha256" not in record, record


@test
def test_docx_release_fields_cover_every_option_read_by_release(tmp: Path):
    """`DOCX_RELEASE_FIELDS` = `DOCX_FIELDS` + `docx_plan`, без дублей и все поля
    попадают в шапку снимка. Проверка схемы не зависит от pandoc."""
    fields = tuple(MK.DOCX_RELEASE_FIELDS)
    assert fields == tuple(MK.DOCX_FIELDS) + ("docx_plan",), fields
    assert len(fields) == len(set(fields)), fields
    assert set(fields) <= set(MK.FIELDS), (fields, MK.FIELDS)
    for name in ("docx_template", "docx_example", "docx_title", "docx_logo",
                 "docx_prompt", "docx_engine", "docx_checks", "docx_map", "docx_plan"):
        assert name in fields, (name, fields)

    root = _project(tmp)
    rel = _source(root)
    _, snap = _snapshot(root, rel)
    head = snap.read_text(encoding="utf-8").split("\n---", 1)[0]
    for name in fields:
        assert f"{name}:" in head, (name, head)


@test
def test_repeat_release_is_idempotent(tmp: Path):
    """Повторный выпуск той же версии не добавляет запись и не меняет docx,
    отчёт и исходник."""
    if not _require_pandoc():
        return
    root = _project(tmp)
    rel = _source(root)
    _snapshot(root, rel)
    first = S.release(str(root), rel, "1.0", "2026-01-02", "", True)
    assert first.get("ok"), first
    record = first["release"]
    docx = root / record["path"]
    report = root / record["checks"]
    docx_bytes, report_bytes = docx.read_bytes(), report.read_bytes()
    source_text = (root / rel).read_text(encoding="utf-8")

    second = S.release(str(root), rel, "1.0", "2026-01-02", "", True)
    assert second.get("ok") and second.get("unchanged"), second
    assert second["release"]["checks"] == record["checks"], second
    assert docx.read_bytes() == docx_bytes, "повторный выпуск пересобрал docx иначе"
    assert report.read_bytes() == report_bytes, "повторный выпуск переписал отчёт"
    assert (root / rel).read_text(encoding="utf-8") == source_text, "повторный выпуск изменил исходник"
    assert len(_records(root, rel)["released_versions"]) == 1, _records(root, rel)


@test
def test_frozen_export_is_byte_identical_and_reports_checks(tmp: Path):
    """`--frozen` на одном `SOURCE_DATE_EPOCH` даёт те же байты, а AURORA-SHIP
    называет неизменяемый отчёт проверок и его статус."""
    if not _require_pandoc():
        return
    root = _project(tmp)
    rel = _source(root)
    _snapshot(root, rel)
    env = dict(os.environ, SOURCE_DATE_EPOCH="1767312000")
    outs = ("Deliverables/opz/frozen-a.docx", "Deliverables/opz/frozen-b.docx")
    runs = []
    for out in outs:
        cmd = [sys.executable, str(root / ".aurora/scripts/ship_doc.py"), rel,
               "--export", "docx", "--frozen", "1.0", "--out", out,
               "--date", "2026-01-02", "--apply"]
        cp = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=300, env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        runs.append(cp)
    assert (root / outs[0]).read_bytes() == (root / outs[1]).read_bytes(), \
        "один снимок и одна дата дали разные docx"
    line = next((row for row in runs[0].stdout.splitlines() if row.startswith("AURORA-SHIP ")), "")
    assert line, runs[0].stdout
    payload = json.loads(line[len("AURORA-SHIP "):])
    assert payload.get("ok") is True, payload
    assert payload.get("checks_status") == "passed", payload
    report_rel = _immutable_checks(payload.get("checks"), root)
    assert payload.get("checks_sha256") == _sha(root / report_rel), payload
    assert payload.get("plan") is None and payload.get("plan_sha256") is None, payload


@test
def test_dry_run_writes_nothing(tmp: Path):
    """План выпуска не создаёт ни снимка, ни docx, ни отчёта, ни записей."""
    root = _project(tmp)
    rel = _source(root)
    before = _tree_digest(root)
    plan = S.release(str(root), rel, "1.0", "2026-01-02", "", False)
    assert plan.get("ok") and plan.get("dry_run"), plan
    assert _tree_digest(root) == before, "dry-run что-то записал"
    assert not list((root / "Deliverables/opz").glob("*.md")), "dry-run создал снимок"
    assert not list((root / "Deliverables/opz").glob("*.docx")), "dry-run создал docx"
    assert not list((root / WORD_CHECKS).glob("check*.json")), "dry-run создал отчёт"
    assert not S.list_records((root / rel).read_text(encoding="utf-8"), "released_versions")

    _snapshot(root, rel)
    frozen = _tree_digest(root)
    plan = S.release(str(root), rel, "1.0", "2026-01-02", "", False)
    assert plan.get("ok") and plan.get("dry_run"), plan
    assert _tree_digest(root) == frozen, "dry-run по готовому снимку что-то записал"


@test
def test_existing_other_docx_is_not_overwritten(tmp: Path):
    """Готовый docx с другими байтами не перезаписывается молча: `docx-conflict`,
    файл остаётся прежним, снимок цел."""
    if not _require_pandoc():
        return
    root = _project(tmp)
    rel = _source(root)
    _snapshot(root, rel)
    out = "Deliverables/opz/conflict.docx"
    first = S.export_frozen(str(root), rel, "1.0", out=out, quiet=True)
    assert first.get("ok"), first
    target = root / out
    target.write_bytes(b"other bytes, not a docx")
    second = S.export_frozen(str(root), rel, "1.0", out=out, quiet=True)
    assert not second.get("ok"), second
    assert second.get("code") == "docx-conflict", second
    assert target.read_bytes() == b"other bytes, not a docx", "чужой docx перезаписан"
