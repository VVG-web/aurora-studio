#!/usr/bin/env python3
"""part_210 — настройки артефактов Code только в разделе Code.

Проверяем, что форма проекта не содержит второго редактора `code_artifacts:`:
она показывает только пометку «управляется в разделе Code», а сам редактор,
API и i18n живут в модуле Code.
"""
from __future__ import annotations

import json

from harness import KIT, test


@test
def test_code_artifact_settings_live_only_in_code(_t):
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    code = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    # В настройках проекта нет второго редактора code-артефактов, только явная отсылка.
    assert "code_artifacts" not in panel, "в panel.js снова появился редактор code_artifacts"
    assert "code.artifacts" not in panel, "в panel.js снова появились поля code.artifacts"
    assert "kinds.code_note" in panel, "в настройках проекта нет пометки, где менять Code"
    # Редактор и сохранение остаются в Code.
    for token in ("/api/code/artifacts", "saveArtifacts", "code.artifacts.save",
                  "code.artifacts.add", "code.artifacts.enabled"):
        assert token in code, token
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/code/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        for key in ("code.artifacts.title", "code.artifacts.path",
                    "code.artifacts.template", "code.artifacts.enabled",
                    "code.artifacts.save", "code.artifacts.add"):
            assert data.get(key), (lang, key)
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        assert data.get("kinds.code_note"), (lang, "kinds.code_note")


@test
def test_code_artifact_defaults_and_api_are_in_code_module(_t):
    source = (KIT / "scripts/code_artifacts.py").read_text(encoding="utf-8")
    assert 'DEFAULT_OUT = "ArtifactsCode"' in source
    assert "def effective_kinds" in source and "def save" in source
    assert 'OVERRIDES = "code_artifacts"' in source


@test
def test_code_auth_fields_offer_ldap_credentials(_t):
    code = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    assert 'c.auth === "ldap"' in code, "LDAP выбран, но поля входа не появляются"
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/code/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        for key in ("code.auth.ldap", "code.h.auth.ldap",
                    "code.h.user.ldap", "code.h.secret_ldap"):
            assert data.get(key), (lang, key)
