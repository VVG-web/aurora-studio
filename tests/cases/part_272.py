"""Проверки движка Aurora, часть 272. Каркас и помощники — tests/harness.py.

Word-предпросмотр 1.231.19 (R5, scope 1A): строгая схема `docx_checks` v1, TOC из
заголовков и его выключение, подключение W5-плана и разрывов страниц, логотип
только по разрешённому хешу, allowlist без лишних элементов/атрибутов, отчёт
`Artifacts/word-checks/check.json` как паспорт preview, а не доказательство
выпуска. Никакой release-механики здесь нет.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from xml.etree import ElementTree as ET
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test
from docfix import docx, large_reference_docx, para

WORD_PREVIEW = Path("Artifacts/word-preview")
WORD_CHECKS = Path("Artifacts/word-checks")


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


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


def _project(tmp: Path, checks: dict | None = None, *, logo: bool = False,
             template: bool = False) -> Path:
    root = make_project(tmp)
    for folder in ("Templates", "Checks", "Artifacts/word-preview", "Artifacts/word-checks"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Раздел\n", encoding="utf-8")
    data = {
        "version": 1, "kind": "opz",
        "required_sections": [{"level": 1, "text": "Введение"}, {"level": 2, "text": "Цель"}],
        "title_fields": {"customer": "ООО Тест"},
        "toc": {"enabled": True, "depth": 2, "title": "Содержание"},
        "page": {"size": "A4", "orientation": "portrait",
                 "margins_mm": {"top": 20, "right": 15, "bottom": 20, "left": 25},
                 "header_mm": 10, "footer_mm": 10},
        "fonts": {"body": "Times New Roman", "heading": "Times New Roman"},
        "images": {"enabled": True, "max_count": 50, "max_bytes": 5242880, "formats": ["png", "jpeg"]},
    }
    if checks:
        data.update(checks)
    (root / "Checks/opz.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    cfg += ('\nartifacts:\n  opz:\n    title: "ОПЗ"\n    template: Templates/AC.md\n'
            '    out: Artifacts/opz\n    docx_checks: Checks/opz.json\n')
    if logo:
        (root / "Checks/logo.png").write_bytes(_png())
        cfg += "    docx_logo: Checks/logo.png\n"
    if template:
        cfg += "    docx_template: Templates/reference.docx\n"
    (root / "aurora.config.yaml").write_text(cfg, encoding="utf-8")
    return root


def _doc(root: Path, body: str | None = None, date: str = "2026-01-02", title: str = "Тестовый документ") -> Path:
    path = root / "Deliverables/work/opz_v1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ("---\ntype: opz\ntitle: \"%s\"\ndate: \"%s\"\nbased_on: \"[ac]\"\n---\n\n"
            "%s\n" % (title, date, body or "# Введение\n\nПервый абзац.\n\n## Цель\n\nВторой абзац.\n"))
    path.write_text(text, encoding="utf-8")
    return path


def _template_with_header_logo(path: Path) -> None:
    """Минимальный .docx: стили и один inline PNG в header — единственный источник логотипа."""
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pr = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", (
            f'<Types xmlns="{ct}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>'))
        z.writestr("_rels/.rels", (
            f'<Relationships xmlns="{pr}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>'))
        z.writestr("word/document.xml", (
            f'<w:document xmlns:w="{w}" xmlns:r="{r}"><w:body><w:p><w:r><w:t>ref</w:t></w:r></w:p>'
            '<w:sectPr><w:headerReference w:type="default" r:id="rId1"/></w:sectPr></w:body></w:document>'))
        z.writestr("word/styles.xml", (
            f'<w:styles xmlns:w="{w}"><w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/><w:basedOn w:val="Normal"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="FirstParagraph"><w:name w:val="First Paragraph"/><w:basedOn w:val="BodyText"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/><w:basedOn w:val="Normal"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="Heading 2"/><w:basedOn w:val="Normal"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="TOCHeading"><w:name w:val="TOC Heading"/><w:basedOn w:val="Normal"/></w:style></w:styles>'))
        z.writestr("word/header1.xml", (
            f'<w:hdr xmlns:w="{w}" xmlns:r="{r}"><w:p><w:r><w:t>header</w:t></w:r></w:p></w:hdr>'))
        z.writestr("word/_rels/document.xml.rels", (
            f'<Relationships xmlns="{pr}"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>'))
        z.writestr("word/_rels/header1.xml.rels", (
            f'<Relationships xmlns="{pr}"><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>'))
        z.writestr("word/media/image1.png", _png())
        z.writestr("docProps/core.xml", (
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>ref</dc:title><dc:creator>Aurora</dc:creator>'
            '<cp:lastModifiedBy>Aurora</cp:lastModifiedBy><cp:revision>1</cp:revision></cp:coreProperties>'))


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(root / ".aurora/scripts/ship_doc.py"),
           "Deliverables/work/opz_v1.md", "--export", "docx", *args]
    return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def _report(root: Path) -> dict:
    return json.loads((root / WORD_CHECKS / "check.json").read_text(encoding="utf-8"))


@test
def test_preview_falls_back_when_distro_pandoc_has_no_embedded_data(tmp: Path):
    """Дистрибутивный pandoc без embed_data_files не умеет docx в песочнице:
    preview повторяет сборку без `--sandbox` и вырезает картинки."""
    if os.name == "nt":
        return  # на Windows kит ставит официальный pandoc со встроенными данными
    real = shutil.which("pandoc")
    if not real:
        return
    root = _project(tmp)
    _doc(root)
    fake = tmp / "fakebin"
    fake.mkdir()
    wrapper = fake / "pandoc"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "--sandbox" ]; then\n'
        '    echo "Could not find data file data/data/docx/[Content_Types].xml" >&2\n'
        "    exit 97\n"
        "  fi\n"
        "done\n"
        f'exec "{real}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = f"{fake}{os.pathsep}{old}"
    try:
        cp = _run(root, "--date", "2026-01-02")
    finally:
        os.environ["PATH"] = old
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert _report(root)["engine"] == "auto", _report(root)
    assert (root / WORD_PREVIEW / "opz_v1.docx").is_file()


@test
def test_docx_example_over_five_mb_is_accepted_for_preview(tmp: Path):
    """Пример 11 МБ читается preview: предел 5 МБ относится к шаблону и эталону,
    а не к `docx_example`; большие части разбирает `docx_check.Reader`."""
    root = _project(tmp)
    (root / "Artifacts/opz").mkdir(parents=True, exist_ok=True)
    example = Path(large_reference_docx(root / "Templates/big-example.docx"))
    assert example.stat().st_size > 5 * 1024 * 1024
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "    docx_example: Templates/big-example.docx\n", encoding="utf-8")
    _doc(root)
    MK = _mod("make_kinds")
    assert MK.check(str(root), MK.read_kinds(str(root))) == []
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (root / WORD_PREVIEW / "opz_v1.docx").is_file()


@test
def test_docx_example_toc_label_is_not_counted_as_leak(tmp: Path):
    """«Содержание» из примера совпадает с названием TOC из docx_checks,
    но это структура документа, а не данные примера."""
    root = _project(tmp)
    example = root / "Templates/example.docx"
    docx(example, para("Содержание") + para("Уникальный текст примера 987654"))
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "    docx_example: Templates/example.docx\n", encoding="utf-8")
    source = _doc(root)
    MK = _mod("make_kinds")
    DC = _mod("docx_check")
    SD = _mod("ship_doc")
    rec = MK.read_kinds(str(root))["opz"]
    expected = DC.load_checks("Checks/opz.json", "opz", str(root))[0]
    needles, _headers = SD._example_needles(
        str(root), rec, source.read_text(encoding="utf-8"), expected)
    assert "Содержание" not in needles, needles
    assert any("Уникальный текст примера" in text for text in needles), needles

    class Reader:
        def all_text(self):
            return "Уникальный текст примера 987654"

        def footer_text(self):
            return ""

    findings = DC._check_example_data(Reader(), {**expected, "example_strings": needles})
    assert [item["code"] for item in findings] == ["docx-example-data"], findings
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert _report(root)["blocked"] is False
    assert "docx-example-data" not in _report(root)["codes"]


@test
def test_docx_checks_v1_is_path_only_and_strict(tmp: Path):
    """`docx_checks` — файл, не inline-строка; version/kind/схема/размер/дубли проверяются до
    экспорта, а `page` переводится в twips той же формулой, что уходит в OOXML."""
    DC = _mod("docx_check")
    root = _project(tmp)
    (root / "Checks/bad.json").write_text(
        '{"version": 1, "version": 1, "kind": "opz", "required_sections": [], "title_fields": {}}', encoding="utf-8")
    expected, found = DC.load_checks("Checks/bad.json", "opz", str(root))
    assert not expected and found[0]["code"] == "docx-checks-json", found

    (root / "Checks/bad.json").write_text(
        '{"version": 2, "kind": "opz", "required_sections": [], "title_fields": {}}', encoding="utf-8")
    assert DC.load_checks("Checks/bad.json", "opz", str(root))[1][0]["code"] == "docx-checks-version"

    (root / "Checks/bad.json").write_text(
        '{"version": 1, "kind": "ac", "required_sections": [], "title_fields": {}}', encoding="utf-8")
    assert DC.load_checks("Checks/bad.json", "opz", str(root))[1][0]["code"] == "docx-checks-kind"

    (root / "Checks/bad.json").write_text(
        '{"version": 1, "kind": "opz", "required_sections": [], "title_fields": {}, "extra": 1}', encoding="utf-8")
    assert DC.load_checks("Checks/bad.json", "opz", str(root))[1][0]["code"] == "docx-checks-schema"

    (root / "Checks/big.json").write_text(" " * (256 * 1024 + 1), encoding="utf-8")
    assert DC.load_checks("Checks/big.json", "opz", str(root))[1][0]["code"] == "docx-checks-size"

    expected, found = DC.load_checks('{"version": 1}', "opz", str(root))
    assert not expected and found[0]["code"] == "docx-checks-json", found
    expected, found = DC.load_checks("Checks/opz.json", "opz", str(root))
    assert not found, found
    assert expected["page"]["margins_twips"] == {"top": 1134, "right": 850, "bottom": 1134, "left": 1417}
    assert expected["toc"]["depth"] == 2


@test
def test_toc_is_built_from_headings_and_can_be_switched_off(tmp: Path):
    """TOC генерирует движок: поле TOC видит `#`/`##`, при `toc.enabled=false` блока TOC и
    `toc_depth` в отчёте нет. Это два разных preview одного документа."""
    root = _project(tmp)
    _doc(root, "# Введение\n\nПервый абзац.\n\n## Цель\n\nВторой абзац.\n\n### Глубже\n\nТретий.\n")
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    target = root / WORD_PREVIEW / "opz_v1.docx"
    assert target.is_file()
    import zipfile
    with zipfile.ZipFile(target) as z:
        document = z.read("word/document.xml").decode("utf-8")
    assert "TOC \\o \"1-2\"" in document and "Введение" in document and "Цель" in document, document[:2000]
    assert "fldChar" in document and "TOC \\o" in document, "TOC не оформлен как поле Word"
    assert "sdt" not in document, "TOC content control не должен проходить allowlist"
    assert "docx-toc" not in {c["code"] for c in _report(root)["checks"]}
    assert _report(root)["toc_depth"] == 2

    checks = json.loads((root / "Checks/opz.json").read_text(encoding="utf-8"))
    checks["toc"] = {"enabled": False, "depth": 3, "title": "Содержание"}
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    cp = _run(root, "--date", "2026-01-02", "--out", "Artifacts/word-preview/no-toc.docx")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    with zipfile.ZipFile(root / WORD_PREVIEW / "no-toc.docx") as z:
        document = z.read("word/document.xml").decode("utf-8")
    assert "fldChar" not in document and "TOC \\o" not in document, document[:2000]
    assert _report(root)["toc_depth"] is None


@test
def test_logo_comes_from_docx_logo_and_hash_is_enforced(tmp: Path):
    """Логотип берётся из `docx_logo`, встраивается в docx, а `images.logo_sha256` сверяется
    до экспорта: чужой хеш — block `docx-logo-missing`, старый preview не перезаписан."""
    root = _project(tmp, logo=True)
    checks = json.loads((root / "Checks/opz.json").read_text(encoding="utf-8"))
    checks["images"]["logo_sha256"] = hashlib.sha256((root / "Checks/logo.png").read_bytes()).hexdigest()
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    _doc(root)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    target = root / WORD_PREVIEW / "opz_v1.docx"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    import zipfile
    with zipfile.ZipFile(target) as z:
        assert any(n.startswith("word/media/") and n.endswith(".png") for n in z.namelist()), z.namelist()
    report = _report(root)
    assert report["logo_source"] == "docx_logo" and report["logo_sha256"] == hashlib.sha256(
        (root / "Checks/logo.png").read_bytes()).hexdigest(), report

    checks = json.loads((root / "Checks/opz.json").read_text(encoding="utf-8"))
    checks["images"]["logo_sha256"] = "0" * 64
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 2 and "docx-logo-missing" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before, "blocked preview перезаписал старый docx"

    checks = json.loads((root / "Checks/opz.json").read_text(encoding="utf-8"))
    checks["images"]["logo_sha256"] = hashlib.sha256((root / "Checks/logo.png").read_bytes()).hexdigest()
    checks["images"]["enabled"] = False
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 2 and "docx-images" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr

    checks = json.loads((root / "Checks/opz.json").read_text(encoding="utf-8"))
    checks["images"]["enabled"] = True
    checks["images"].pop("logo_sha256", None)
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 2 and "docx-logo-missing" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before


@test
def test_logo_falls_back_to_template_header(tmp: Path):
    """Нет `docx_logo` — ровно один PNG из header шаблона становится логотипом,
    если его sha256 разрешён в `docx_checks.images.logo_sha256`; без хеша — block."""
    root = _project(tmp)
    _template_with_header_logo(root / "Templates/reference.docx")
    with zipfile.ZipFile(root / "Templates/reference.docx") as z:
        logo_sha = hashlib.sha256(z.read("word/media/image1.png")).hexdigest()
    checks = json.loads((root / "Checks/opz.json").read_text(encoding="utf-8"))
    checks["images"]["logo_sha256"] = logo_sha
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    cfg = cfg.replace("    docx_checks: Checks/opz.json\n",
                      "    docx_checks: Checks/opz.json\n    docx_template: Templates/reference.docx\n")
    (root / "aurora.config.yaml").write_text(cfg, encoding="utf-8")
    _doc(root)
    cp = _run(root, "--date", "2026-01-02", "--docx-engine", "reference")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    report = _report(root)
    assert report["logo_source"] == "docx_template" and report["logo_sha256"] == logo_sha
    with zipfile.ZipFile(root / WORD_PREVIEW / "opz_v1.docx") as z:
        assert "word/media/image1.png" in z.namelist(), z.namelist()


@test
def test_plan_engine_without_w5_plan_is_blocked(tmp: Path):
    """`docx_engine=plan` в 1.231.19 не имеет плана W5: preview обязан отказать
    `docx-plan-missing`, а не молча собрать документ по эталону."""
    root = _project(tmp)
    _doc(root)
    cp = _run(root, "--date", "2026-01-02", "--docx-engine", "plan")
    assert cp.returncode == 2 and "docx-plan-missing" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    assert not (root / WORD_PREVIEW / "opz_v1.docx").exists()


@test
def test_plan_engine_reads_w5_plan_and_applies_page_break(tmp: Path):
    """`docx_engine=plan` читает `Scripts/Generated/<вид>.docx.json`, сверяет
    source_sha256 и применяет `page_break_before` к соответствующему заголовку."""
    root = _project(tmp)
    source = _doc(root)
    plan = {
        "version": 1, "kind": "opz",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "sections": [
            {"heading": "Введение", "level": 1, "page_break_before": False},
            {"heading": "Цель", "level": 2, "page_break_before": True},
        ],
        "toc_depth": 2,
    }
    plan_path = root / "Scripts/Generated/opz.docx.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    cp = _run(root, "--date", "2026-01-02", "--docx-engine", "plan")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    report = _report(root)
    assert report["engine"] == "plan" and report["docx_plan"] == "Scripts/Generated/opz.docx.json", report
    assert report["plan_sha256"] and report["plan_sections"] == 2, report
    with zipfile.ZipFile(root / WORD_PREVIEW / "opz_v1.docx") as z:
        document = z.read("word/document.xml").decode("utf-8")
    assert document.count("pageBreakBefore") == 1, document
    assert "pageBreakBefore" in document[:document.find("Цель")], document[:4000]

    cp = _run(root, "--date", "2026-01-02", "--docx-engine", "auto",
              "--out", "Artifacts/word-preview/auto.docx")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert _report(root)["docx_plan"] == "Scripts/Generated/opz.docx.json"

    plan["source_sha256"] = "0" * 64
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    cp = _run(root, "--date", "2026-01-02", "--docx-engine", "plan")
    assert cp.returncode == 2 and "docx-plan-stale" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr


@test
def test_allowlist_rejects_extra_element_and_attribute(tmp: Path):
    """Чужой элемент и атрибут не проходят R5 allowlist даже в готовом preview."""
    root = _project(tmp)
    _doc(root)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    good = root / WORD_PREVIEW / "opz_v1.docx"
    DC = _mod("docx_check")
    expected, found = DC.load_checks("Checks/opz.json", "opz", str(root))
    assert not found, found
    bad = root / WORD_PREVIEW / "bad.docx"
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "word/document.xml":
                xml = ET.fromstring(data)
                body = xml.find("{%s}body" % w)
                first_p = body.find("{%s}p" % w)
                first_p.set("{%s}anchor" % w, "1")
                ET.SubElement(body, "{%s}ins" % w)
                data = ET.tostring(xml, encoding="utf-8", xml_declaration=True)
            dst.writestr(name, data)
    findings = DC.check(str(bad), expected)
    codes = {item["code"] for item in findings}
    messages = " ".join(item["message"] for item in findings)
    assert "docx-xml-not-allowed" in codes, findings
    assert "anchor" in messages and "ins" in messages, messages


@test
def test_allowlist_rejects_cross_namespace_elements_and_attributes(tmp: Path):
    """`a:p`, `r:p`, `wp:p`, `pic:p`, `a:t`, `a:drawing` и `{a}val` на `w:p`
    не проходят: обычный элемент WordprocessingML живёт только в W namespace,
    атрибут — только в namespace своего элемента."""
    root = _project(tmp)
    _doc(root)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    good = root / WORD_PREVIEW / "opz_v1.docx"
    DC = _mod("docx_check")
    expected, found = DC.load_checks("Checks/opz.json", "opz", str(root))
    assert not found, found
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    foreign = [
        ("http://schemas.openxmlformats.org/drawingml/2006/main", "p"),
        ("http://schemas.openxmlformats.org/officeDocument/2006/relationships", "p"),
        ("http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing", "p"),
        ("http://schemas.openxmlformats.org/drawingml/2006/picture", "p"),
        ("http://schemas.openxmlformats.org/drawingml/2006/main", "t"),
        ("http://schemas.openxmlformats.org/drawingml/2006/main", "drawing"),
    ]
    bad = root / WORD_PREVIEW / "bad-ns.docx"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "word/document.xml":
                xml = ET.fromstring(data)
                body = xml.find("{%s}body" % w)
                first_p = body.find("{%s}p" % w)
                first_p.set("{http://schemas.openxmlformats.org/drawingml/2006/main}val", "1")
                for ns, local in foreign:
                    ET.SubElement(body, "{%s}%s" % (ns, local))
                data = ET.tostring(xml, encoding="utf-8", xml_declaration=True)
            dst.writestr(name, data)
    findings = DC.check(str(bad), expected)
    codes = [item["code"] for item in findings]
    messages = " ".join(item["message"] for item in findings)
    assert "docx-xml-not-allowed" in codes, findings
    assert messages.count("элемент p не разрешён") == 4, messages
    assert "элемент t не разрешён" in messages, messages
    assert "элемент drawing не разрешён" in messages, messages
    assert "атрибут val не разрешён у p" in messages, messages

    with zipfile.ZipFile(bad) as z:
        pruned = ET.fromstring(z.read("word/document.xml"))
    DC._prune(pruned, "document")
    body = pruned.find("{%s}body" % w)
    foreign_ns = {"http://schemas.openxmlformats.org/drawingml/2006/main",
                  "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                  "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
                  "http://schemas.openxmlformats.org/drawingml/2006/picture"}
    for el in body.iter():
        assert not any(el.tag.startswith("{%s}" % ns) for ns in foreign_ns), el.tag
    first_p = body.find("{%s}p" % w)
    assert first_p is not None and first_p.get(
        "{http://schemas.openxmlformats.org/drawingml/2006/main}val") is None


@test
def test_page_orientation_and_margins_come_from_checks(tmp: Path):
    """`page.orientation=landscape` меняет местами ширину и высоту, а поля переводятся
    в twips; OOXML не остаётся с дефолтным портретным `sectPr`."""
    root = _project(tmp, checks={
        "page": {"size": "Letter", "orientation": "landscape",
                 "margins_mm": {"top": 10, "right": 12, "bottom": 10, "left": 18},
                 "header_mm": 8, "footer_mm": 8},
    })
    _doc(root)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    import zipfile
    with zipfile.ZipFile(root / WORD_PREVIEW / "opz_v1.docx") as z:
        document = z.read("word/document.xml").decode("utf-8")
    assert 'w="15840"' in document and 'h="12240"' in document, document[-1500:]
    assert 'top="567"' in document and 'right="680"' in document and 'left="1020"' in document, document[-1500:]


@test
def test_preview_report_is_a_passport_not_a_release(tmp: Path):
    """`check.json` лежит в `Artifacts/word-checks/`, содержит хеши docx и checks, дату,
    версию pandoc, коды и пути; рядом с исходником и в `Deliverables/released` ничего нет."""
    root = _project(tmp)
    _doc(root)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    report = _report(root)
    assert report["blocked"] is False and report["sha256"] == hashlib.sha256(
        (root / WORD_PREVIEW / "opz_v1.docx").read_bytes()).hexdigest()
    assert report["docx_checks_sha256"] == hashlib.sha256(
        (root / "Checks/opz.json").read_bytes()).hexdigest()
    assert report["date"] == "2026-01-02T00:00:00Z" and report["source_date_epoch"] == 1767312000
    assert report["pandoc"].startswith("pandoc ")
    assert report["paths"]["docx"].endswith("Artifacts/word-preview/opz_v1.docx")
    assert report["paths"]["check"] == "Artifacts/word-checks/check.json"
    assert report["codes"] == [] or all(isinstance(c, str) for c in report["codes"])
    assert report["logo_source"] is None and report["logo_sha256"] is None
    released = root / "Deliverables/released"
    assert not released.exists() or not any(released.iterdir())
    assert not (root / "Deliverables/work/opz_v1.docx").exists()
