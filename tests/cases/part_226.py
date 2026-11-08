#!/usr/bin/env python3
"""part_226 - когда изменения требуют расширенного RAGAS-прогона."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from harness import KIT, test

sys.path.insert(0, str(KIT / "tests"))
import ragas_impact as RI  # noqa: E402


@test
def test_retrieval_changes_require_ragas_and_design_does_not(_t):
    for path in ("scripts/answer_core.py", "scripts/production_core.py",
                 "scripts/agent_runner.py", "scripts/ctx_pack.py",
                 "scripts/context_runtime.py", "scripts/kb_search_quality.py",
                 "scripts/sources_registry.py", "cockpit/aurora_cockpit.py"):
        assert RI.classify([path])["required"] is True, path
    for path in ("cockpit/modules/ask/i18n/ru.json", "cockpit/modules/work/i18n/en.json",
                 "cockpit/modules/ask/view.html", "cockpit/modules/work/view.html",
                 "cockpit/skins/zine.css", "docs/CONTRIBUTING.md"):
        assert RI.classify([path])["required"] is False, path
    mixed = RI.classify(["cockpit/skins/zine.css", "scripts/answer_core.py"])
    assert mixed["required"] is True and mixed["design_only"] is False, mixed
    assert RI.classify([])["required"] is False


@test
def test_ragas_runner_is_lazy_and_uses_env_not_internal_addresses(_t):
    script = (KIT / "tests/ragas_eval.py").read_text(encoding="utf-8")
    assert "RAGAS_LLM_BASE_URL" in script and "RAGAS_EMBED_BASE_URL" in script
    assert "192.168." not in script, "в публичный тест попал внутренний адрес"
    assert "from ragas" not in script.split("def main", 1)[0], "RAGAS импортируется до разбора аргументов"
    cp = subprocess.run([sys.executable, str(KIT / "tests/ragas_eval.py"), "--help"],
                        cwd=str(KIT), capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=30)
    assert cp.returncode == 0 and "--project" in cp.stdout, (cp.stdout, cp.stderr)
    req = (KIT / "tests/requirements-ragas.txt").read_text(encoding="utf-8")
    assert "ragas==0.4.3" in req and "langchain-community==0.3.31" in req


@test
def test_rule_is_recorded_in_both_contributing_guides(_t):
    for path in ("docs/CONTRIBUTING.md", "docs/en/CONTRIBUTING.md"):
        text = (KIT / path).read_text(encoding="utf-8")
        assert "tests/ragas_eval.py" in text and "RAGAS" in text, path
        assert "Спросить" in text or "Ask" in text, path
        assert "Продуктивност" in text or "Productivity" in text, path


@test
def test_new_code_messages_have_both_translations(_t):
    view = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    keys = set(re.findall(r"code\.(?:add|detect)\.[a-z_]+", view))
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/code/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        assert not [k for k in keys if not data.get(k)], (lang, sorted(keys))
