"""Проверки движка Aurora, часть 275. Каркас и помощники — tests/harness.py.

Word-предпросмотр 1.231.19 end-to-end и Windows: детерминированный docx по
`SOURCE_DATE_EPOCH`, `--date`/`date:` из шапки, запрет текущего времени, `--out`
только в `Artifacts/word-preview/`, запрет сырого OOXML, титул из XML. Release-
механики B/C здесь нет и быть не должно.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from harness import SCRIPTS, make_project, test

WORD_PREVIEW = Path("Artifacts/word-preview")


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _project(tmp: Path) -> Path:
    root = make_project(tmp)
    for folder in ("Templates", "Checks", "Artifacts/word-preview", "Artifacts/word-checks"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Раздел\n", encoding="utf-8")
    checks = {
        "version": 1, "kind": "opz",
        "required_sections": [{"level": 1, "text": "Введение"}],
        "title_fields": {"customer": "ООО Тест"},
        "toc": {"enabled": True, "depth": 2, "title": "Содержание"},
        "page": {"size": "A4", "orientation": "portrait",
                 "margins_mm": {"top": 20, "right": 15, "bottom": 20, "left": 25},
                 "header_mm": 10, "footer_mm": 10},
        "fonts": {"body": "Times New Roman", "heading": "Times New Roman"},
        "images": {"enabled": True, "max_count": 50, "max_bytes": 5242880, "formats": ["png", "jpeg"]},
    }
    (root / "Checks/opz.json").write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    cfg += ('\nartifacts:\n  opz:\n    title: "ОПЗ"\n    template: Templates/AC.md\n'
            '    out: Artifacts/opz\n    docx_checks: Checks/opz.json\n')
    (root / "aurora.config.yaml").write_text(cfg, encoding="utf-8")
    return root


def _doc(root: Path, body: str = "# Введение\n\nПервый абзац.\n", date: str | None = "2026-01-02") -> Path:
    path = root / "Deliverables/work/opz_v1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = '---\ntype: opz\ntitle: "Тестовый документ"\nbased_on: "[ac]"\n'
    if date is not None:
        head += 'date: "%s"\n' % date
    path.write_text(head + "---\n\n" + body, encoding="utf-8")
    return path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(root / ".aurora/scripts/ship_doc.py"),
           "Deliverables/work/opz_v1.md", "--export", "docx", *args]
    return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _core(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("docProps/core.xml").decode("utf-8")


@test
def test_preview_is_reproducible_and_has_no_release_side_effect(tmp: Path):
    """Один `--date` — одинаковый sha256 независимо от порядка запуска; другой `--date` —
    другой sha256. Core.xml, ZIP и `check.json` пишутся только в разрешённые папки,
    `Deliverables/released` не появляется."""
    root = _project(tmp)
    _doc(root)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    first = root / WORD_PREVIEW / "opz_v1.docx"
    one = _sha(first)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0 and _sha(first) == one, cp.stdout + cp.stderr
    core = _core(first)
    assert "Aurora Studio" in core and "2026-01-02T00:00:00Z" in core, core
    with zipfile.ZipFile(first) as z:
        dates = {i.date_time for i in z.infolist()}
    assert dates == {(2026, 1, 2, 0, 0, 0)}, dates

    cp = _run(root, "--date", "2026-01-03", "--out", "Artifacts/word-preview/other.docx")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert _sha(root / WORD_PREVIEW / "other.docx") != one
    released = root / "Deliverables/released"
    assert not released.exists() or not any(released.iterdir())
    assert not (root / "Deliverables/work/opz_v1.docx").exists()


@test
def test_date_comes_from_cli_or_header_and_never_from_now(tmp: Path):
    """`--date` сильнее `date:`; без обоих preview блокируется `docx-date`, и снять его
    через `--allow` нельзя. RFC3339 с `+03:00` нормализуется в UTC."""
    root = _project(tmp)
    _doc(root, date="2026-02-03")
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "2026-01-02T00:00:00Z" in _core(root / WORD_PREVIEW / "opz_v1.docx")

    cp = _run(root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "2026-02-03T00:00:00Z" in _core(root / WORD_PREVIEW / "opz_v1.docx")

    _doc(root, date=None)
    cp = _run(root)
    assert cp.returncode == 2 and "docx-date" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    cp = _run(root, "--allow", "docx-date")
    assert cp.returncode == 2 and "invalid choice" in (cp.stderr + cp.stdout), cp.stdout + cp.stderr

    _doc(root, date="2026-02-03")
    cp = _run(root, "--date", "2026-01-02T03:00:00+03:00", "--out", "Artifacts/word-preview/utc.docx")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "2026-01-02T00:00:00Z" in _core(root / WORD_PREVIEW / "utc.docx")


@test
def test_out_is_confined_to_the_preview_folder(tmp: Path):
    """`--out` вне `Artifacts/word-preview/` — отказ до pandoc; симлинк из preview наружу
    тоже не обходит границу."""
    root = _project(tmp)
    _doc(root)
    cp = _run(root, "--date", "2026-01-02", "--out", "Deliverables/work/x.docx")
    assert cp.returncode == 2 and "preview пишется только" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    assert not (root / "Deliverables/work/x.docx").exists()

    if not hasattr(os, "symlink"):
        return
    link = root / WORD_PREVIEW / "outside"
    try:
        link.symlink_to(root / "Deliverables/work", target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    cp = _run(root, "--date", "2026-01-02", "--out", "Artifacts/word-preview/outside/x.docx")
    assert cp.returncode == 2 and "preview пишется только" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    assert not (root / "Deliverables/work/x.docx").exists()


@test
def test_raw_ooxml_is_blocked_and_allow_cannot_lift_it(tmp: Path):
    """Сырой OOXML из markdown — `docx-raw-ooxml`, неснимаемый код: preview не пишется,
    `--allow docx-raw-ooxml` отвергается ещё разбором аргументов."""
    root = _project(tmp)
    _doc(root, "# Введение\n\n<w:p>чужой OOXML</w:p>\n")
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 2 and "docx-raw-ooxml" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    assert not (root / WORD_PREVIEW / "opz_v1.docx").exists()
    cp = _run(root, "--date", "2026-01-02", "--allow", "docx-raw-ooxml")
    assert cp.returncode == 2 and "invalid choice" in (cp.stderr + cp.stdout), cp.stdout + cp.stderr
    assert not (root / WORD_PREVIEW / "opz_v1.docx").exists()


@test
def test_title_xml_is_escaped_and_substituted(tmp: Path):
    """Титул `.xml` проходит sanitizer W2, подстановки `{{...}}` заполняются из
    `title_fields`, текст попадает в тело и core.xml; разметка титула не становится
    сырым OOXML."""
    root = _project(tmp)
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    (root / "Checks/title.xml").write_text(
        '<w:document xmlns:w="%s"><w:body>'
        '<w:p><w:r><w:t>{{customer}}</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>ОПЗ &amp; приложение</w:t></w:r></w:p>'
        '</w:body></w:document>' % w, encoding="utf-8")
    _doc(root)
    cp = _run(root, "--date", "2026-01-02", "--docx-title", "Checks/title.xml")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    with zipfile.ZipFile(root / WORD_PREVIEW / "opz_v1.docx") as z:
        document = z.read("word/document.xml").decode("utf-8")
    assert "ООО Тест" in document and "ОПЗ &amp; приложение" in document, document[:3000]
    assert "2026-01-02T00:00:00Z" in _core(root / WORD_PREVIEW / "opz_v1.docx")


@test
def test_raw_title_is_rejected_by_w2_sanitizer(tmp: Path):
    """Титул с элементом вне allowlist W2 не принимается как raw XML: preview
    останавливается `docx-title-unsupported` и не перезаписывает готовый docx."""
    root = _project(tmp)
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    (root / "Checks/raw.xml").write_text(
        '<w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>сырой титул</w:t></w:r></w:p>'
        '<w:ins/></w:body></w:document>' % w, encoding="utf-8")
    _doc(root)
    cp = _run(root, "--date", "2026-01-02", "--docx-title", "Checks/raw.xml")
    assert cp.returncode == 2 and "docx-title-unsupported" in (cp.stdout + cp.stderr), cp.stdout + cp.stderr
    assert not (root / WORD_PREVIEW / "opz_v1.docx").exists()


@test
def test_reference_with_mixed_namespaces_is_still_a_preview(tmp: Path):
    """Pandoc с эталоном может смешать `ns0:` и `w:` в `styles.xml`; preview обязан
    починить пространства имён, а не падать трассировкой."""
    root = _project(tmp)
    _doc(root)
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    (root / "Templates/reference.docx").write_bytes((root / WORD_PREVIEW / "opz_v1.docx").read_bytes())
    cp = _run(root, "--date", "2026-01-02", "--docx-engine", "reference",
              "--reference", "Templates/reference.docx", "--out", "Artifacts/word-preview/ref.docx")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (root / WORD_PREVIEW / "ref.docx").is_file()
    with zipfile.ZipFile(root / WORD_PREVIEW / "ref.docx") as z:
        assert "word/styles.xml" in z.namelist()


@test
def test_preview_handles_lists_code_and_footnotes(tmp: Path):
    """Allowlist не режет обычный markdown: списки, код, сноски и выделения проходят,
    а не превращаются в `docx-xml-not-allowed`."""
    root = _project(tmp)
    _doc(root, "# Введение\n\n**Жирный** и *курсив*.\n\n- один\n- два\n\n"
              "```\nprint(1)\n```\n\nСноска.[^1]\n\n[^1]: Текст сноски.\n")
    cp = _run(root, "--date", "2026-01-02")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    with zipfile.ZipFile(root / WORD_PREVIEW / "opz_v1.docx") as z:
        document = z.read("word/document.xml").decode("utf-8")
        assert "footnoteReference" in document or "footnote" in z.namelist(), z.namelist()


@test
def test_windows_backslash_out_and_rfc3339(tmp: Path):
    """Windows: `--out` с обратными слэшами остаётся внутри preview. На POSIX проверяем
    ту же нормализацию даты, чтобы поведение не расходилось по платформам."""
    root = _project(tmp)
    _doc(root)
    if os.name == "nt":
        cp = _run(root, "--date", "2026-01-02", "--out", "Artifacts\\word-preview\\win.docx")
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert (root / WORD_PREVIEW / "win.docx").is_file()
    cp = _run(root, "--date", "2026-01-02T05:30:00Z", "--out", "Artifacts/word-preview/z.docx")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "2026-01-02T05:30:00Z" in _core(root / WORD_PREVIEW / "z.docx")
