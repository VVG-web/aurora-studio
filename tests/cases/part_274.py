"""Проверки части 274: декларативный Word-план W5.

План это только JSON: разделы, разрывы и глубина оглавления. Модуль W5 не
исполняет модельный код и не зависит от W1/W3. Проверяем схему v1, хеши,
stale по всем входам, runner и интеграцию через импорт.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

from harness import SCRIPTS, make_project, test

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

DP = importlib.import_module("docx_plan")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan(source: str, **extra) -> dict:
    plan = {
        "version": 1,
        "kind": "opz",
        "source_sha256": source,
        "sections": [
            {"heading": "Введение", "level": 1, "page_break_before": False},
            {"heading": "Заключение", "level": 1, "page_break_before": True},
        ],
    }
    plan.update(extra)
    return plan


def _raises(code: str, fn) -> None:
    try:
        fn()
    except DP.PlanError as exc:
        assert exc.code == code, (exc.code, exc.why)
    else:
        raise AssertionError(f"ожидалась ошибка {code}")


def _checks(*, toc_enabled: bool = True, toc_depth: int = 3,
            logo_sha256: str | None = None) -> dict:
    return {
        "version": 1,
        "kind": "opz",
        "required_sections": [],
        "title_fields": {},
        "toc": {"enabled": toc_enabled, "depth": toc_depth,
                "title": "Содержание"},
        "page": {
            "size": "A4",
            "orientation": "portrait",
            "margins_mm": {"top": 20, "right": 15, "bottom": 20, "left": 25},
            "header_mm": 10,
            "footer_mm": 10,
        },
        "fonts": {"body": "Times New Roman", "heading": "Times New Roman"},
        "images": {"enabled": True, "max_count": 50, "max_bytes": 5242880,
                   "formats": ["png", "jpeg"], "logo_sha256": logo_sha256},
    }


def _fixture(root: Path, *, toc_enabled: bool = True,
             toc_depth: int = 3) -> tuple[dict, dict, str, str]:
    """Проект с исходником, checks, prompt, template, example, reference, logo и планом."""
    files = {
        "source": "Sources/opz.md",
        "docx_checks": "Prompts/opz_checks.json",
        "prompt": "Prompts/opz_word.md",
        "template": "Templates/OPZ.dotx",
        "example": "Templates/OPZ-example.docx",
        "reference": "Templates/reference.docx",
        "logo": "Templates/logo.png",
    }
    logo_bytes = b"\x89PNG\r\n\x1a\nlogo"
    checks_data = _checks(toc_enabled=toc_enabled, toc_depth=toc_depth,
                          logo_sha256=_sha(logo_bytes))
    payloads = {
        "source": b"---\ndate: 2026-11-24\n---\n\n# OPZ\n\nText\n",
        "docx_checks": json.dumps(checks_data, ensure_ascii=False).encode("utf-8"),
        "prompt": b"Word prompt\n",
        "template": b"template bytes",
        "example": b"example bytes",
        "reference": b"reference bytes",
        "logo": logo_bytes,
    }
    for key, rel in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payloads[key])
    docx_path = "Scripts/Generated/opz.docx"
    (root / docx_path).parent.mkdir(parents=True, exist_ok=True)
    (root / docx_path).write_bytes(b"docx preview bytes")
    plan = _plan(_sha(payloads["source"]), toc_depth=toc_depth,
                 notes=["Не выводить в docx"])
    plan_path = "Prompts/opz.docx.json"
    (root / plan_path).write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    inputs = dict(files)
    return inputs, plan, plan_path, docx_path


def _run(root: Path, plan_path: str, inputs: dict, docx_path: str, **extra):
    return DP.run_plan(
        root, "opz", plan_path, checks=inputs["docx_checks"],
        docx_path=docx_path, inputs=inputs, pandoc_version="3.13", **extra)


@test
def test_docx_checks_v1_schema_and_toc(tmp: Path):
    """docx_checks v1: version, kind, toc и дубликаты проверяются отдельно."""
    root = make_project(tmp)
    checks = _checks()
    got = DP.validate_checks(checks, kind="opz")
    assert got["toc"] == {"enabled": True, "depth": 3, "title": "Содержание"}, got
    assert got["page"]["margins_mm"]["left"] == 25, got
    bad = dict(checks, version=2)
    _raises("docx-checks-version", lambda: DP.validate_checks(bad))
    _raises("docx-checks-kind",
            lambda: DP.validate_checks(checks, kind="ac"))
    bad_toc = dict(checks, toc={"enabled": True, "depth": 4})
    _raises("docx-checks-schema", lambda: DP.validate_checks(bad_toc))
    checks_path = root / "Prompts" / "duplicate-checks.json"
    checks_path.write_text(
        json.dumps(checks, ensure_ascii=False)[:-1] + ', "version": 1}',
        encoding="utf-8")
    _raises("docx-checks-json", lambda: DP.load_checks(checks_path))


@test
def test_docx_plan_schema_v1_accepts_json_and_rejects_code(tmp: Path):
    """Валидный план проходит, неизвестное поле script отвергается без исполнения."""
    root = make_project(tmp)
    _inputs, plan, _plan_path, _docx = _fixture(root)
    got = DP.validate_plan(plan, kind="opz", toc_depth=3)
    assert got["version"] == 1 and got["kind"] == "opz", got
    assert len(got["sections"]) == 2, got
    assert got["notes"] == ["Не выводить в docx"], got

    bad = dict(plan, script="print('run me')")
    _raises("docx-plan-schema", lambda: DP.validate_plan(bad))
    bad = dict(plan, landscape=["Приложение"])
    _raises("docx-plan-schema", lambda: DP.validate_plan(bad))
    bad = dict(plan, sections=[{"heading": "H", "level": 1, "page_break_before": 1}])
    _raises("docx-plan-schema", lambda: DP.validate_plan(bad))
    bad = dict(plan, version=2)
    _raises("docx-plan-schema", lambda: DP.validate_plan(bad))
    _raises("docx-plan-kind", lambda: DP.validate_plan(plan, kind="ac"))
    _raises("docx-plan-source", lambda: DP.validate_plan(plan, source_sha256="0" * 64))

    raw = json.dumps(plan, ensure_ascii=False)
    duplicate = raw[:-1] + ', "version": 1}'
    _raises("docx-plan-json", lambda: DP.loads_plan(duplicate))
    _raises("docx-plan-json", lambda: DP.loads_plan(raw[:-1] + ', "x": NaN}'))
    assert DP.loads_plan(raw)["kind"] == "opz"


@test
def test_docx_plan_toc_depth_and_disabled_toc(tmp: Path):
    """TOC depth сверяется с checks; при enabled=false глубина игнорируется."""
    root = make_project(tmp)
    inputs, plan, plan_path, docx_path = _fixture(root)
    _raises("docx-plan-toc-depth",
            lambda: DP.validate_plan(plan, toc_depth=2))
    got = DP.validate_plan(plan, toc_enabled=False, toc_depth=2)
    assert got["toc_depth"] == 3, got
    assert DP.effective_toc_depth(plan, toc_enabled=False) is None
    assert DP.effective_toc_depth(plan, toc_enabled=True, checks_depth=1) == 3
    no_depth = dict(plan)
    no_depth.pop("toc_depth")
    assert DP.effective_toc_depth(no_depth, checks_depth=2) == 2
    assert DP.effective_toc_depth(no_depth) == DP.DEFAULT_TOC_DEPTH
    checks_path = root / inputs["docx_checks"]
    checks_path.write_text(
        json.dumps(_checks(toc_enabled=False,
                           logo_sha256=_sha((root / inputs["logo"]).read_bytes())),
                   ensure_ascii=False),
        encoding="utf-8")
    got = _run(root, plan_path, inputs, docx_path, toc_depth=3)
    meta = json.loads(Path(got["meta"]).read_text(encoding="utf-8"))
    assert got["toc_depth"] is None and meta["toc_depth"] is None, (got, meta)


@test
def test_docx_plan_runner_writes_plan_and_meta(tmp: Path):
    """Runner пишет канонический план и meta, не требуя W1/W3."""
    root = make_project(tmp)
    inputs, plan, plan_path, docx_path = _fixture(root)
    got = _run(root, plan_path, inputs, docx_path, toc_depth=3,
               logo_source="docx_logo")
    plan_file = Path(got["plan"])
    meta_file = Path(got["meta"])
    assert plan_file.resolve() == (root / "Scripts" / "Generated" / "opz.docx.json").resolve(), plan_file
    assert meta_file.resolve() == (root / "Scripts" / "Generated" / "opz.docx.meta.json").resolve(), meta_file
    assert plan_file.is_file() and meta_file.is_file()
    written = DP.load_plan(plan_file)
    assert written == plan, (written, plan)

    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert DP.validate_meta(meta) is meta
    assert meta["version"] == 1 and meta["kind"] == "opz", meta
    assert meta["toc_depth"] == 3, meta
    assert meta["docx_sha256"] == _sha(b"docx preview bytes"), meta
    assert meta["date"] == "2026-11-24T00:00:00Z", meta
    assert meta["source_date_epoch"] == DP.parse_meta_date("2026-11-24")[1], meta
    assert meta["pandoc_version"] == "3.13", meta
    assert meta["margins_mm"] == {"top": 20, "right": 15, "bottom": 20, "left": 25}, meta
    assert meta["margins_twips"] == {"top": 1134, "right": 850, "bottom": 1134,
                                     "left": 1417}, meta
    assert meta["header_twips"] == 567 and meta["footer_twips"] == 567, meta
    assert meta["docx_checks_sha256"] == _sha(
        (root / inputs["docx_checks"]).read_bytes()), meta
    for field in ("docx_sha256", "date", "source_date_epoch", "pandoc_version",
                  "margins_mm", "margins_twips", "header_mm", "header_twips",
                  "footer_mm", "footer_twips", "source_sha256",
                  "docx_checks_sha256", "plan_sha256"):
        assert meta[field] is not None, (field, meta)
    assert meta["logo_source"] == "docx_logo", meta
    assert meta["logo_sha256"] == _sha(b"\x89PNG\r\n\x1a\nlogo"), meta
    checks = json.loads((root / inputs["docx_checks"]).read_text(encoding="utf-8"))
    assert meta["logo_sha256"] == checks["images"]["logo_sha256"], meta
    bad_meta = dict(meta, logo_sha256=None)
    _raises("docx-meta-schema", lambda: DP.validate_meta(bad_meta))
    assert meta["plan_sha256"] == DP.plan_digest(written), meta
    assert meta["source_sha256"] == plan["source_sha256"], meta


@test
def test_docx_plan_logo_requires_checks_hash_and_matches_file(tmp: Path):
    """logo_source без разрешения checks и расхождение хеша блокируются."""
    root = make_project(tmp)
    inputs, _plan, plan_path, docx_path = _fixture(root)
    checks_path = root / inputs["docx_checks"]
    checks = json.loads(checks_path.read_text(encoding="utf-8"))

    mismatch = json.loads(json.dumps(checks))
    mismatch["images"]["logo_sha256"] = "0" * 64
    checks_path.write_text(json.dumps(mismatch, ensure_ascii=False), encoding="utf-8")
    _raises("docx-plan-logo",
            lambda: _run(root, plan_path, inputs, docx_path,
                         logo_source="docx_logo"))

    checks_path.write_text(json.dumps(checks, ensure_ascii=False), encoding="utf-8")
    logo_path = root / inputs["logo"]
    original = logo_path.read_bytes()
    logo_path.write_bytes(original + b"changed")
    try:
        _raises("docx-plan-logo",
                lambda: _run(root, plan_path, inputs, docx_path,
                             logo_source="docx_logo"))
    finally:
        logo_path.write_bytes(original)

    unauthorized = json.loads(json.dumps(checks))
    unauthorized["images"].pop("logo_sha256")
    checks_path.write_text(json.dumps(unauthorized, ensure_ascii=False),
                           encoding="utf-8")
    _raises("docx-plan-logo",
            lambda: _run(root, plan_path, inputs, docx_path,
                         logo_source="docx_logo"))


@test
def test_docx_plan_stale_by_every_hash(tmp: Path):
    """Изменение любого входа, плана, toc_depth или logo_source делает паспорт stale."""
    root = make_project(tmp)
    inputs, plan, plan_path, docx_path = _fixture(root)
    got = _run(root, plan_path, inputs, docx_path, toc_depth=3,
               logo_source="docx_logo")
    meta = json.loads(Path(got["meta"]).read_text(encoding="utf-8"))
    base_plan = DP.load_plan(root / plan_path)

    def current(p: dict = None) -> dict:
        cur = DP.collect_hashes(root, inputs, plan=p or base_plan)
        cur["toc_depth"] = 3
        cur["logo_source"] = "docx_logo"
        cur["date"] = meta["date"]
        cur["pandoc_version"] = meta["pandoc_version"]
        return cur

    assert DP.stale_reasons(meta, current()) == []
    for key, rel in inputs.items():
        path = root / rel
        original = path.read_bytes()
        path.write_bytes(original + b"changed")
        try:
            reasons = DP.stale_reasons(meta, current())
        finally:
            path.write_bytes(original)
        assert any(reason.startswith(key + ":") for reason in reasons), (key, reasons)

    changed_plan = dict(base_plan, notes=["changed"])
    assert "plan: hash changed" in DP.stale_reasons(meta, current(changed_plan))
    cur = current()
    cur["toc_depth"] = 1
    assert "toc_depth: changed" in DP.stale_reasons(meta, cur)
    cur = current()
    cur["logo_source"] = "docx_template"
    assert "logo_source: changed" in DP.stale_reasons(meta, cur)
    cur = current()
    cur["date"] = "2026-11-25T00:00:00Z"
    assert "date: changed" in DP.stale_reasons(meta, cur)
    cur = current()
    cur["pandoc_version"] = "3.14"
    assert "pandoc_version: changed" in DP.stale_reasons(meta, cur)
    assert DP.is_stale(meta, cur)


@test
def test_docx_plan_paths_missing_source_and_out_dir_are_blocked(tmp: Path):
    """Путь вне проекта, отсутствующий вход и неверный source блокируются."""
    root = make_project(tmp)
    inputs, plan, plan_path, docx_path = _fixture(root)
    _raises("docx-plan-path",
            lambda: _run(root, plan_path, inputs, docx_path,
                         out_dir="../outside"))
    _raises("docx-plan-path",
            lambda: _run(root, "../outside.json", inputs, docx_path))
    bad_inputs = dict(inputs, prompt="../outside.md")
    _raises("docx-plan-path",
            lambda: _run(root, plan_path, bad_inputs, docx_path))
    missing = dict(inputs)
    missing.pop("prompt")
    missing["prompt"] = "Prompts/no-such.md"
    _raises("docx-plan-missing-input",
            lambda: _run(root, plan_path, missing, docx_path))
    bad_plan = dict(plan, source_sha256="0" * 64)
    bad_path = "Prompts/bad.docx.json"
    (root / bad_path).write_text(json.dumps(bad_plan, ensure_ascii=False),
                                 encoding="utf-8")
    _raises("docx-plan-source",
            lambda: _run(root, bad_path, inputs, docx_path))


@test
def test_docx_plan_cli_and_no_executable_code(tmp: Path):
    """CLI запускает runner; модуль не содержит eval/exec, notes остаются данными."""
    root = make_project(tmp)
    inputs, plan, plan_path, docx_path = _fixture(root)
    source = Path(DP.__file__).read_text(encoding="utf-8")
    assert "exec(" not in source and "eval(" not in source, "в модуле появился исполняемый код"
    code = dict(plan, notes=["__import__('os').system('touch should-not-exist')"])
    code_path = "Prompts/code.docx.json"
    (root / code_path).write_text(json.dumps(code, ensure_ascii=False),
                                  encoding="utf-8")
    got = _run(root, code_path, inputs, docx_path, toc_depth=3)
    assert not (root / "should-not-exist").exists()
    assert DP.load_plan(got["plan"])["notes"] == code["notes"]

    cp = subprocess.run([
        sys.executable, str(SCRIPTS / "docx_plan.py"),
        "--project", str(root), "--kind", "opz", "--plan", plan_path,
        "--source", inputs["source"], "--checks", inputs["docx_checks"],
        "--docx", docx_path, "--pandoc-version", "3.13",
        "--toc-depth", "3",
    ], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
        errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    out = json.loads(cp.stdout)
    assert out["ok"] is True and Path(out["plan"]).is_file(), out
