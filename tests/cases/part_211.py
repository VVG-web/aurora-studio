#!/usr/bin/env python3
"""part_211 — GitLab-группы Code: модель, discovery, новые репозитории.

Сеть подменена: проверяем пагинацию, `include_subgroups`, авторизацию без утечки
токена, 14-дневный сброс подсветки и добавление выбранных проектов в `code_repos`.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
sys.path.insert(0, str(KIT / "cockpit"))
import code_groups as CG  # noqa: E402
import code_repos as CR  # noqa: E402
import git_sync as GS  # noqa: E402
import aurora_cockpit as CK  # noqa: E402

GROUP_URL = "https://gitlab.example.com/rf/team"


def _project(path: str, branch: str = "main") -> dict:
    return {"id": abs(hash(path)) % 100000, "path": path, "path_with_namespace": path,
            "name": path.rsplit("/", 1)[-1],
            "http_url_to_repo": f"https://gitlab.example.com/{path}.git", "ssh_url_to_repo": "",
            "default_branch": branch, "created_at": "2026-01-02T00:00:00Z",
            "last_activity_at": "2026-01-03T00:00:00Z", "web_url": f"https://gitlab.example.com/{path}"}


def _fake_http(pages: list, seen: list):
    def http(url, headers, timeout):
        seen.append({"url": url, "headers": dict(headers), "timeout": timeout})
        import re
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        data = pages[page - 1] if page <= len(pages) else []
        hdrs = {"x-next-page": str(page + 1)} if page < len(pages) else {}
        return 200, data, "", hdrs
    return http


@test
def test_group_block_round_trip_keeps_neighbours(_t):
    root = make_project(_t)
    path = root / "aurora.config.yaml"
    path.write_text(path.read_text(encoding="utf-8")
                    + "\n# чужой блок ниже\ncustom: keep\n"
                    + "\ncode_repos:\n  legacy:\n    url: \"https://git.example.com/t/legacy.git\"\n"
                    + "    provider: \"github\"\n    branch: \"\"\n", encoding="utf-8")
    groups = {"team": {"url": GROUP_URL, "include_subgroups": True,
                       "selected": ["rf/team/front"], "known": ["rf/team/front", "rf/team/api"]}}
    res = CG.save(root, groups, CG.state(root)["digest"])
    assert res.get("ok"), res
    raw = path.read_text(encoding="utf-8")
    assert "custom: keep" in raw and "code_repos:" in raw and "legacy" in raw, raw
    assert "code_groups:" in raw and "include_subgroups: true" in raw, raw
    got = CG.parse(raw)["team"]
    assert got["selected"] == ["rf/team/front"], got
    assert got["known"] == ["rf/team/api", "rf/team/front"], got  # writer сортирует lists
    assert CG.state(root)["digest"] == res["digest"]
    stale = CG.save(root, groups, "stale")
    assert stale.get("conflict") and stale.get("ok") is not True, stale


@test
def test_discovery_paginates_subgroups_and_marks_new(_t):
    root = make_project(_t)
    seen = []
    CG._http = lambda: _fake_http([[_project("rf/team/api"), _project("rf/team/front")],
                                   [_project("rf/team/sub/deep")]], seen)
    group = {"url": GROUP_URL, "include_subgroups": True, "known": ["rf/team/api"]}
    res = CG.fetch_projects(str(root), "team", group,
                            {"auth": "token", "user": "", "secret": "tok-secret"})
    assert res.get("ok"), res
    assert len(res["projects"]) == 3, res["projects"]
    assert [p["path"] for p in res["projects"]] == ["rf/team/api", "rf/team/front", "rf/team/sub/deep"]
    assert [p["new"] for p in res["projects"]] == [False, True, True], res["projects"]
    assert res["new"] == 2 and res["auth"] is True, res
    assert len(seen) == 2, seen
    assert "%2F" in seen[0]["url"] and "include_subgroups=true" in seen[0]["url"], seen[0]
    assert "per_page=100" in seen[0]["url"] and "page=2" not in seen[0]["url"], seen[0]
    assert "page=2" in seen[1]["url"], seen[1]
    assert seen[0]["headers"].get("PRIVATE-TOKEN") == "tok-secret", seen[0]["headers"]
    assert "tok-secret" not in json.dumps(res), "токен утёк в ответ"
    seen.clear()
    CG._http = lambda: _fake_http([[_project("rf/team/api")]], seen)
    flat = CG.fetch_projects(str(root), "team", {"url": GROUP_URL, "include_subgroups": False},
                             {"auth": "token", "user": "", "secret": "x"})
    assert flat.get("ok") and "include_subgroups=false" in seen[0]["url"], (flat, seen)


@test
def test_new_highlight_resets_after_fourteen_days(_t):
    root = make_project(_t)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat().replace("+00:00", "Z")
    seen = []
    CG._http = lambda: _fake_http([[_project("rf/team/api"), _project("rf/team/front")]], seen)
    group = {"url": GROUP_URL, "include_subgroups": True,
             "known": ["rf/team/api"], "last_seen_at": old}
    res = CG.fetch_projects(str(root), "team", group)
    assert res.get("ok"), res
    assert res["reset"] is True, res
    assert res["new"] == 0 and [p["new"] for p in res["projects"]] == [False, False], res
    assert res["group"]["known"] == ["rf/team/api", "rf/team/front"], res["group"]


@test
def test_selection_updates_code_repos_and_keeps_unselected_new(_t):
    root = make_project(_t)
    p1, p2 = _project("rf/team/front"), _project("rf/team/api")
    # Сохраняем группу до выбора: known пуст, оба проекта новые.
    seed = {"team": {"url": GROUP_URL, "include_subgroups": True,
                     "selected": [], "known": [], "repo_names": []}}
    assert CG.save(root, seed, "").get("ok")
    before = CR.digest(CR.read_repos(root))
    res = CG.save_selection(str(root), "team", GROUP_URL, ["rf/team/front"],
                            repo_expect=before, projects=[p1, p2],
                            credentials={"auth": "token", "user": "", "secret": "tok-secret"})
    assert res.get("ok"), res
    repos = CR.read_repos(root)
    assert list(repos) == ["front"], repos
    assert repos["front"]["url"] == p1["http_url_to_repo"], repos
    assert repos["front"]["provider"] == "gitlab" and repos["front"]["branch"] == "main", repos
    raw = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "tok-secret" not in raw and "code_groups:" in raw, raw
    state = CG.read_groups(root)["team"]
    assert state["known"] == ["rf/team/front"] and state["repo_names"] == ["front"], state
    # Невыбранный api остаётся новым при следующем discovery.
    seen = []
    CG._http = lambda: _fake_http([[p1, p2]], seen)
    again = CG.fetch_projects(str(root), "team", state)
    assert [(r["path"], r["new"]) for r in again["projects"]] == [
        ("rf/team/front", False), ("rf/team/api", True)], again["projects"]
    # Снятие выбора убирает запись, которую добавила группа.
    res2 = CG.save_selection(str(root), "team", GROUP_URL, [], repo_expect=CR.digest(CR.read_repos(root)),
                             projects=[p1, p2])
    assert res2.get("ok"), res2
    assert CR.read_repos(root) == {}, CR.read_repos(root)


@test
def test_group_token_is_kept_outside_config_and_reused(_t):
    root = make_project(_t)
    p1 = _project("rf/team/front")
    assert CG.save(root, {"team": {**CG.blank(), "url": GROUP_URL}}, "").get("ok")
    res = CG.save_selection(str(root), "team", GROUP_URL, ["rf/team/front"],
                            repo_expect=CR.digest(CR.read_repos(root)), projects=[p1],
                            credentials={"auth": "token", "user": "", "secret": "tok-secret"})
    assert res.get("ok"), res
    assert "tok-secret" not in (root / "aurora.config.yaml").read_text(encoding="utf-8")
    saved = GS.cred_load(str(root), CG.cred_remote("team", GROUP_URL))
    assert saved["secret"] == "tok-secret" and saved["auth"] == "token", saved
    headers, cred = CG._auth_headers(str(root), "team", url=GROUP_URL)
    assert headers.get("PRIVATE-TOKEN") == "tok-secret" and cred.get("secret") == "tok-secret", headers
    other, _ = CG._auth_headers(str(root), "team", url="https://other.example.com/rf/team")
    assert not other, "токен группы ушёл на другой хост"


@test
def test_group_api_reports_conflicts_and_never_returns_secret(_t):
    root = make_project(_t)
    old_d, old_s = CG.discover, CG.save_selection
    try:
        CG.discover = lambda *_a, **_k: {"ok": True, "projects": [], "group": {"name": "team"},
                                         "new": 0, "auth": True}
        res, code = CK.code_group_action("discover", str(root), {"name": "team", "url": GROUP_URL})
        assert code == 200 and res.get("ok"), res
        CG.save_selection = lambda *_a, **_k: {"ok": False, "conflict": True, "error": "conflict"}
        res, code = CK.code_group_action("save", str(root), {"name": "team", "url": GROUP_URL,
                                                              "selected": [], "repo_expect": "stale"})
        assert code == 409 and res.get("conflict"), res
        bad, code = CK.code_group_action("discover", str(root), {"name": "bad name", "url": GROUP_URL})
        assert code == 400 and bad.get("field") == "name", bad
    finally:
        CG.discover, CG.save_selection = old_d, old_s


@test
def test_group_api_discovers_and_saves_selection(_t):
    root = make_project(_t)
    seen = []
    old_http = CG._http
    CG._http = lambda: _fake_http([[_project("rf/team/front"), _project("rf/team/api")]], seen)
    try:
        res, code = CK.code_group_action("discover", str(root), {
            "name": "team", "url": GROUP_URL, "include_subgroups": True,
            "credentials": {"auth": "token", "user": "", "secret": "tok-secret"}})
        assert code == 200 and res.get("ok"), res
        assert [p["new"] for p in res["projects"]] == [True, True], res
        saved, code = CK.code_group_action("save", str(root), {
            "name": "team", "url": GROUP_URL, "include_subgroups": True,
            "selected": ["rf/team/front"], "projects": res["projects"],
            "repo_expect": CR.digest(CR.read_repos(str(root))),
            "credentials": {"auth": "token", "user": "", "secret": "tok-secret"}})
        assert code == 200 and saved.get("ok"), saved
        assert list(CR.read_repos(str(root))) == ["front"], CR.read_repos(str(root))
        assert "code_groups:" in (root / "aurora.config.yaml").read_text(encoding="utf-8")
    finally:
        CG._http = old_http


@test
def test_group_ui_and_catalogs_are_wired(_t):
    view = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    for token in ("groupsCard", "/api/code/groups/discover", "/api/code/groups/save",
                  "GSEL", "code.groups.new", "code.groups.save_n"):
        assert token in view, token
    import re
    keys = sorted(set(re.findall(r"code\.groups\.[a-z_]+", view)))
    assert keys, "в Code UI нет ключей групп"
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/code/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        missing = [k for k in keys if not data.get(k)]
        assert not missing, (lang, missing)
