#!/usr/bin/env python3
"""part_224 - одна ссылка Code сама отличает репозиторий от папки GitLab.

Проверяем API-разбор без записи в проект: публичная группа, проект, явный `.git`,
дубль и неизвестный приватный адрес. Сеть подменена.
"""
from __future__ import annotations

import sys

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
sys.path.insert(0, str(KIT / "cockpit"))
import code_groups as CG  # noqa: E402
import code_repos as CR  # noqa: E402
import aurora_cockpit as CK  # noqa: E402

GROUP_URL = "https://gitlab.example.com/rf/team"


def _http_group():
    calls = []

    def http(url, headers, timeout):
        calls.append(url)
        if "/projects/" in url:
            return 404, None, "HTTP 404", {}
        if "/groups/" in url and "/projects?" in url:
            return 200, [{"id": 1, "path": "rf/team/api", "path_with_namespace": "rf/team/api",
                          "name": "api", "http_url_to_repo": "https://gitlab.example.com/rf/team/api.git",
                          "default_branch": "main", "created_at": "2026-01-01T00:00:00Z",
                          "last_activity_at": "2026-01-02T00:00:00Z"}], "", {}
        if "/groups/" in url:
            return 200, {"id": 7, "full_path": "rf/team", "name": "team"}, "", {}
        return 500, None, "unexpected", {}

    return calls, http


@test
def test_group_url_becomes_folder_with_projects(_t):
    root = make_project(_t)
    before = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    calls, http = _http_group()
    old = CG._http
    try:
        CG._http = lambda: http
        res = CG.detect(str(root), GROUP_URL, {"auth": "token", "user": "", "secret": "tok"})
    finally:
        CG._http = old
    assert res.get("ok") and res.get("kind") == "group", res
    assert res.get("group_name") == "team", res
    assert [p["path"] for p in res.get("projects", [])] == ["rf/team/api"], res
    assert res.get("auth") is True
    assert "tok" not in str(res), "секрет не должен возвращаться в UI"
    assert (root / "aurora.config.yaml").read_text(encoding="utf-8") == before, "detect не пишет проект"
    assert any("/projects/" in c for c in calls) and any("/groups/" in c for c in calls), calls


@test
def test_gitlab_project_is_repository_and_duplicate_is_named(_t):
    root = make_project(_t)
    rec = {**CR.blank(), "url": "https://gitlab.example.com/rf/team/api.git", "provider": "gitlab"}
    saved = CR.save(root, {"api": rec})
    assert saved.get("ok"), saved
    calls = []

    def http(url, headers, timeout):
        calls.append(url)
        return 200, {"id": 1, "path_with_namespace": "rf/team/api"}, "", {}

    old = CG._http
    try:
        CG._http = lambda: http
        res = CG.detect(str(root), "https://gitlab.example.com/rf/team/api")
    finally:
        CG._http = old
    assert res.get("kind") == "repo" and res.get("duplicate") == "api", res
    assert calls == ["https://gitlab.example.com/api/v4/projects/rf%2Fteam%2Fapi"], calls


@test
def test_existing_folder_is_reused_with_its_selection(_t):
    root = make_project(_t)
    groups = {"team": {"url": GROUP_URL, "include_subgroups": True,
                       "selected": ["rf/team/api"], "known": ["rf/team/api"]}}
    assert CG.save(root, groups, CG.state(root)["digest"]).get("ok")
    _calls, http = _http_group()
    old = CG._http
    try:
        CG._http = lambda: http
        res = CG.detect(str(root), GROUP_URL)
    finally:
        CG._http = old
    assert res.get("kind") == "group" and res.get("existing") is True, res
    assert res.get("group_name") == "team" and res.get("selected") == ["rf/team/api"], res


@test
def test_explicit_git_url_skips_network_and_unknown_asks_for_auth(_t):
    root = make_project(_t)
    old = CG._http
    try:
        CG._http = lambda: (_ for _ in ()).throw(AssertionError("сеть для .git не нужна"))
        res = CG.detect(str(root), "git clone https://gitlab.example.com/rf/team/api.git")
        assert res.get("kind") == "repo", res
        CG._http = lambda: (lambda url, headers, timeout: (401, None, "HTTP 401", {}))
        res = CG.detect(str(root), "https://gitlab.example.com/rf/private")
    finally:
        CG._http = old
    assert res.get("kind") == "unknown" and res.get("error") == "gitlab_auth", res


@test
def test_detect_endpoint_is_wired(_t):
    root = make_project(_t)
    old = CG.detect
    try:
        CG.detect = lambda project, text, credentials=None: {"ok": True, "kind": "repo", "url": text}
        res, code = CK.code_detect_action(str(root), {"text": "https://git.example.com/x.git"})
    finally:
        CG.detect = old
    assert code == 200 and res.get("kind") == "repo", (res, code)
    view = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    for token in ("/api/code/parse", "kind === \"group\"", "kind === \"repo\"", "code.add.detect",
                  "codeAddUrl", "code.add.plus_hint", "authFields(ctx, GCRED"):
        assert token in view, token
    assert view.count('id: "codeAddUrl"') == 1, "должно быть одно поле ссылки"
