#!/usr/bin/env python3
"""part_223 - в Code у GitLab-группы снова выбирается способ входа.

Проверяем две вещи: форма группы показывает системный вход, токен, логин-пароль
и LDAP, а backend для LDAP/пароля действительно отправляет Basic, а не токен.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
import code_groups as CG  # noqa: E402

GROUP_URL = "https://gitlab.example.com/rf/team"


@test
def test_group_form_has_auth_choice_and_ldap_fields(_t):
    view = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    assert "GCRED" in view and "authFields(ctx, GCRED" in view, "у группы нет выбора входа"
    assert 'auths: ["system", "token", "password", "ldap"]' in view, "SSH предложен там, где API его не умеет"
    assert "GTOKEN" not in view, "старое поле «только токен» осталось"
    for key in ("code.auth.system", "code.auth.token", "code.auth.password", "code.auth.ldap",
                "code.field.auth", "code.field.user"):
        assert key in view or key in (KIT / "cockpit/modules/code/i18n/ru.json").read_text(encoding="utf-8"), key


@test
def test_group_backend_uses_basic_for_ldap_and_password(_t):
    root = make_project(Path(_t))
    for auth in ("ldap", "password"):
        headers, cred = CG._auth_headers(str(root), "team", {
            "auth": auth, "user": "ivan", "secret": "pw"}, GROUP_URL)
        assert "Authorization" in headers and "PRIVATE-TOKEN" not in headers, (auth, headers)
        raw = headers["Authorization"].split(" ", 1)[1]
        assert base64.b64decode(raw).decode() == "ivan:pw", headers
        assert cred.get("secret") == "pw" and cred.get("user") == "ivan"
    headers, _ = CG._auth_headers(str(root), "team", {
        "auth": "token", "user": "", "secret": "tok"}, GROUP_URL)
    assert headers.get("PRIVATE-TOKEN") == "tok", headers
    headers, _ = CG._auth_headers(str(root), "team", {
        "auth": "ldap", "user": "", "secret": "pw"}, GROUP_URL)
    assert headers == {}, "пароль LDAP без логина нельзя отправлять как токен"
