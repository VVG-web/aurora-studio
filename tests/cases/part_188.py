#!/usr/bin/env python3
"""part_188 — преднастроенные артефакты по коду (TemplatesCode/PromptCode).

Проверяем, что кит привозит готовые виды, проект переопределяет их файлом с тем же
`kind`, `--check` ловит пропавший промпт и негодную папку, а настройки проекта для
видов не нужны.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_artifacts as CA  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _template(kind: str, enabled: str = "true", out: str = "Artifacts/contracts/code",
              prompt: str = "") -> str:
    prompt = prompt or f"PromptCode/{kind}.md"
    return (f"---\nkind: {kind}\ntitle: \"{kind}\"\nversion: \"1.0\"\n"
            f"enabled: \"{enabled}\"\nscope: route\nout: {out}\nprompt: {prompt}\n"
            "purity: traceable\norder: 200\n---\n\n"
            f"# {kind}\n\n## Что не удалось установить по коду\n")


@test
def test_code_artifacts_presets_and_project_override(_t):
    """Кит даёт 13 видов, 9 включены; проект переопределяет вид и добавляет свой."""
    root = make_project(_t)
    assert (root / "TemplatesCode").is_dir() and (root / "PromptCode").is_dir()
    cp = run("code_artifacts.py", "--project", str(root), "--list", cwd=root, expect_rc=0)
    assert "cr-api" in cp.stdout and "cr-screen" in cp.stdout, cp.stdout
    assert "Видов: 13; включено: 9" in cp.stdout, cp.stdout
    assert "cr-api" in run("code_artifacts.py", "--project", str(root), "--show", "cr-api",
                          cwd=root, expect_rc=0).stdout
    _write(root / "TemplatesCode" / "cr-api_v2.0.md", _template("cr-api", "false", "CleanRoom/code/api"))
    _write(root / "PromptCode" / "cr-api.md", "Свой промпт проекта.\n")
    _write(root / "TemplatesCode" / "my-doc_v1.0.md", _template("my-doc", "true", "Artifacts/drafts/code"))
    _write(root / "PromptCode" / "my-doc.md", "Свой промпт нового вида.\n")
    data = json.loads(run("code_artifacts.py", "--project", str(root), "--json",
                         cwd=root, expect_rc=0).stdout)
    assert data["kinds"]["cr-api"]["enabled"] is False, data["kinds"]["cr-api"]
    assert data["kinds"]["cr-api"]["out"] == "CleanRoom/code/api", data["kinds"]["cr-api"]
    assert data["kinds"]["cr-api"]["source"] == "project", data["kinds"]["cr-api"]
    assert data["kinds"]["my-doc"]["enabled"] is True, data["kinds"]["my-doc"]
    assert data["problems"] == [], data["problems"]


@test
def test_code_artifacts_check_names_missing_prompt_and_bad_out(_t):
    """Проверка называет промпт без файла и папку вне Artifacts/CleanRoom."""
    root = make_project(_t)
    _write(root / "TemplatesCode" / "cr-bad_v1.0.md", _template("cr-bad", "true", "../outside"))
    cp = run("code_artifacts.py", "--project", str(root), "--check", cwd=root, expect_rc=1)
    assert "no_prompt" in cp.stdout, cp.stdout
    assert "out" in cp.stdout, cp.stdout
    rows, problems = CA.check(root)
    assert any(p["kind"] == "cr-bad" and p["code"] == "no_prompt" for p in problems), problems
    assert any(p["kind"] == "cr-bad" and p["code"] == "out" for p in problems), problems


@test
def test_installed_engine_finds_kit_templates_via_kit_path(_t):
    """Проект с пустым TemplatesCode видит шаблоны кита через .aurora/kit_path.txt."""
    root = make_project(_t)
    (root / ".aurora" / "kit_path.txt").write_text(str(KIT), encoding="utf-8")
    cp = subprocess.run(
        [sys.executable, str(root / ".aurora/scripts/code_artifacts.py"),
         "--project", str(root), "--json"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60)
    assert cp.returncode == 0, cp.stderr
    data = json.loads(cp.stdout)
    assert "cr-api" in data["kinds"] and "cr-screen" in data["kinds"], data
    assert data["kinds"]["cr-api"]["prompt_exists"] is True, data["kinds"]["cr-api"]
    assert data["problems"] == [], data["problems"]
