"""Проверки движка Aurora, часть 165: окно выбора пути — список папки на сервере панели («Выбор вместо ввода»).

Каркас и помощники — tests/harness.py. Путь к папке или файлу человек выбирает в окне панели, а список одного
уровня отдаёт `GET /api/fs/list` (`fs_list`, только чтение). Проект — пути от корня, граница та же, что у «Файлов»:
выход за корень и ссылка наружу — отказ, `.git`, `.aurora`, служебное и файлы с доступами не показываются. Машина —
абсолютные пути от домашней папки (здесь — временной, не настоящей), скрытое — по просьбе или внутри скрытой папки
(`~/.ssh`), файлы — только когда выбирают файл. Папка без прав — понятная причина, а не 500.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401


def _ck():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


def _names(res: dict) -> list:
    return [e["name"] for e in res["entries"]]


def _project(tmp: Path) -> tuple:
    """Проект с обычными папками, служебным, секретами и ссылками внутрь и наружу. → (корень, удались ли ссылки):
    на Windows без прав на ссылки их нет, и проверки ссылок пропускаются."""
    root = tmp / "prj"
    for d in ("Templates", "Prompts", "Artifacts/us", "Raw/contract", ".git", ".aurora/state", ".claude",
              "local", "node_modules/x", "Code/demo/src", "Code/demo/.git"):
        (root / d).mkdir(parents=True)
    for f, text in (("Templates/US.md", "# US"), ("Prompts/us.md", "p"), ("a.md", "abc"), (".env", "X=1"),
                    ("id_rsa", "k"), ("server.pem", "k"), (".editorconfig", "root = true"), ("Raw/contract/c.md", "c"),
                    (".git/config", "[core]")):
        (root / f).write_text(text, encoding="utf-8")
    (tmp / "outside").mkdir()
    (tmp / "outside" / "x.md").write_text("чужое", encoding="utf-8")
    try:
        (root / "out-link").symlink_to(tmp / "outside", target_is_directory=True)
        (root / "in-link").symlink_to(root / "Raw", target_is_directory=True)
    except (OSError, NotImplementedError):
        return root, False
    return root, True


@test
def test_project_listing_is_one_level_folders_first_without_service_and_secrets(tmp: Path):
    """Корень проекта: сначала папки, потом файлы, по имени; пути — от корня; `.git`, `.aurora`, `.claude`, `local`,
    `node_modules`, `.env`, ключи и ссылка наружу не видны, ссылка внутрь проекта видна. `want=dir` — одни папки;
    `hidden=1` открывает обычный файл с точки, но не служебное и не секреты."""
    ck = _ck()
    root, links = _project(tmp)
    res = ck.fs_list("project", str(root), "")
    assert res.get("error") is None, res
    assert _names(res) == ["Artifacts", "Code"] + (["in-link"] if links else []) + ["Prompts", "Raw", "Templates",
                                                                                     "a.md"], _names(res)
    assert res["path"] == "" and res["parent"] is None, res
    files = [e for e in res["entries"] if e["kind"] == "file"]
    assert files == [{"name": "a.md", "kind": "file", "size": 3, "path": "a.md"}], files
    assert res["crumbs"] == [{"path": "", "name": "prj"}], res["crumbs"]

    sub = ck.fs_list("project", str(root), "Templates", "file")
    assert sub["path"] == "Templates" and sub["parent"] == "", sub
    assert [e["path"] for e in sub["entries"]] == ["Templates/US.md"], sub["entries"]
    assert [c["name"] for c in sub["crumbs"]] == ["prj", "Templates"], sub["crumbs"]

    dirs = ck.fs_list("project", str(root), "", "dir")
    assert all(e["kind"] == "dir" for e in dirs["entries"]) and "a.md" not in _names(dirs), dirs["entries"]

    shown = _names(ck.fs_list("project", str(root), "", "any", hidden=True))
    assert ".editorconfig" in shown, shown
    for never in (".git", ".aurora", ".claude", "local", "node_modules", ".env", "id_rsa", "server.pem", "out-link"):
        assert never not in shown, (never, shown)

    clone = ck.fs_list("project", str(root), "Code/demo", "dir")
    assert _names(clone) == ["src"], clone           # `.git` клона скрыт и на глубине


@test
def test_project_listing_refuses_to_leave_the_root_by_dots_or_by_a_link(tmp: Path):
    """`..`, путь через ссылку наружу и путь в служебное — отказ с кодом и причиной; файл открывает свою папку с ним
    выбранным, путь, которого нет, — ближайшую существующую папку проекта."""
    ck = _ck()
    root, links = _project(tmp)
    for bad in ("..", "../outside", "Templates/../../outside") + (("out-link", "out-link/x.md") if links else ()):
        res = ck.fs_list("project", str(root), bad)
        assert res.get("code") == "outside" and res.get("error"), (bad, res)
        assert "entries" not in res, (bad, res)
    for svc in (".git", ".aurora/state", "local", ".env", "id_rsa"):
        res = ck.fs_list("project", str(root), svc)
        assert res.get("code") == "service", (svc, res)
    if links:
        inner = ck.fs_list("project", str(root), "in-link")
        assert inner.get("error") is None and _names(inner) == ["contract"], inner
    picked = ck.fs_list("project", str(root), "Templates/US.md", "file")
    assert picked["path"] == "Templates" and picked["selected"] == "US.md", picked
    gone = ck.fs_list("project", str(root), "Artifacts/us/2026/draft.md")
    assert gone["path"] == "Artifacts/us" and gone["selected"] == "", gone
    assert ck.fs_list("elsewhere", str(root), "").get("code") == "scope"


@test
def test_machine_listing_lives_in_the_temporary_home(tmp: Path):
    """Машина: пустой путь — домашняя папка (временная), пути абсолютные, цепочка начинается с «~». Файлы — только
    при выборе файла; скрытое — по `hidden` или внутри скрытой папки (`~/.ssh`), там файл ключа виден сразу; путь к
    файлу открывает его папку с ним выбранным; путь, которого нет, — ближайшую существующую папку над ним."""
    ck = _ck()
    home = tmp / "home"
    for d in ("Documents/work", ".ssh", ".config"):
        (home / d).mkdir(parents=True)
    for f in (".ssh/id_ed25519", ".ssh/id_ed25519.pub", ".ssh/known_hosts", "notes.txt"):
        (home / f).write_text("x", encoding="utf-8")
    restore = set_home(home)
    try:
        res = ck.fs_list("machine", "", "", "dir")
        assert res.get("error") is None, res
        assert res["path"] == str(home) and res["home"] == str(home), (res["path"], res["home"])
        assert _names(res) == ["Documents"], _names(res)
        assert res["crumbs"][0] == {"path": str(home), "name": "~"}, res["crumbs"]
        assert res["roots"][0] == {"path": str(home), "name": "~", "kind": "home"}, res["roots"]
        assert [e["path"] for e in res["entries"]] == [str(home / "Documents")], res["entries"]

        assert _names(ck.fs_list("machine", "", "", "file")) == ["Documents", "notes.txt"]
        assert _names(ck.fs_list("machine", "", "", "dir", hidden=True)) == [".config", ".ssh", "Documents"]

        ssh = ck.fs_list("machine", "", "~/.ssh", "file")
        assert ssh["hidden"] is True and _names(ssh) == ["id_ed25519", "id_ed25519.pub", "known_hosts"], ssh
        assert [c["name"] for c in ssh["crumbs"]] == ["~", ".ssh"], ssh["crumbs"]
        key = ck.fs_list("machine", "", str(home / ".ssh" / "id_ed25519"), "file")
        assert key["path"] == str(home / ".ssh") and key["selected"] == "id_ed25519", key
        deep = ck.fs_list("machine", "", "~/Documents/work/nope/deeper", "dir")
        assert deep["path"] == str(home / "Documents" / "work"), deep
        rel = ck.fs_list("machine", "", "Documents", "dir")       # не абсолютный — от домашней папки
        assert rel["path"] == str(home / "Documents"), rel
    finally:
        restore()


@test
def test_machine_roots_windows_branch_runs_harmlessly_anywhere(tmp: Path):
    """Корни машины: домашняя папка первой; ветка Windows на любой машине перебирает буквы дисков без вреда (на
    этой их нет — остаётся дом), с подставной проверкой — диски по порядку; ветка macOS без `/Volumes` — дом и «/»."""
    ck = _ck()
    restore = set_home(tmp)
    try:
        win = ck.fs_machine_roots("Windows")
        assert win[0] == {"path": str(tmp), "name": "~", "kind": "home"}, win
        assert all(r["kind"] == "drive" and len(r["path"]) == 3 and r["path"].endswith(":\\") for r in win[1:]), win
        fake = ck.fs_machine_roots("Windows", exists=lambda p: p in ("D:\\", "C:\\"))
        assert [(r["name"], r["path"], r["kind"]) for r in fake[1:]] == [("C:", "C:\\", "drive"),
                                                                         ("D:", "D:\\", "drive")], fake
        mac = ck.fs_machine_roots("Darwin", exists=lambda p: False)
        assert [r["kind"] for r in mac] == ["home", "root"], mac
    finally:
        restore()


@test
def test_a_folder_without_rights_gives_a_reason_not_a_500(tmp: Path):
    """Папка без прав (подменённый `os.listdir` бросает PermissionError): `fs_list` — код `denied` и причина с путём;
    живой сервер панели отвечает 403 с той же причиной (по-английски — в английском режиме), а не 500 и не обрывом.
    Проект вне списка обнаруженных сервер не открывает; без проекта — отказ, а не список чужой папки."""
    from cases.part_16 import _Panel
    ck = _ck()
    restore = set_home(tmp / "home")
    (tmp / "home" / "locked").mkdir(parents=True)
    (tmp / "parent").mkdir()
    project = make_project(tmp / "parent")
    (project / "Raw" / "locked").mkdir(parents=True, exist_ok=True)
    locked = {str(tmp / "home" / "locked"), str(project / "Raw" / "locked")}
    real = os.listdir

    def listdir(path="."):
        if os.path.realpath(str(path)) in {os.path.realpath(p) for p in locked}:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)
    os.listdir = listdir
    panel = _Panel([tmp / "parent"])
    try:
        res = ck.fs_list("machine", "", str(tmp / "home" / "locked"), "dir")
        assert res.get("code") == "denied" and str(tmp / "home" / "locked") in res.get("error", ""), res
        res = ck.fs_list("project", str(project), "Raw/locked", "dir")
        assert res.get("code") == "denied" and "Raw/locked" in res.get("error", ""), res

        import http.client
        from urllib.parse import urlencode

        def get(query: dict):
            c = http.client.HTTPConnection("127.0.0.1", panel.srv.server_address[1], timeout=60)
            c.request("GET", "/api/fs/list?" + urlencode(query), headers={"X-Aurora-Token": panel.ck.TOKEN})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode("utf-8"))
        status, body = get({"scope": "machine", "path": str(tmp / "home" / "locked"), "want": "dir"})
        assert status == 403 and body.get("code") == "denied" and "нет доступа" in body.get("error", ""), (status, body)
        status, body = get({"scope": "project", "project": str(project), "path": "Raw/locked", "lang": "en"})
        assert status == 403 and body.get("error", "").startswith("no access to the folder"), (status, body)
        status, body = get({"scope": "project", "project": str(project), "path": "../..", "lang": "en"})
        assert status == 403 and body.get("code") == "outside" and body.get("error") == "the path leaves the project", body
        status, body = get({"scope": "project", "project": str(project), "path": "Raw"})
        assert status == 200 and "locked" in [e["name"] for e in body["entries"]], (status, body)
        status, body = get({"scope": "project", "project": str(tmp / "home"), "path": ""})
        assert status == 400 and "entries" not in body, (status, body)
        status, body = get({"scope": "project", "path": ""})
        assert status == 400 and "entries" not in body, (status, body)
    finally:
        os.listdir = real
        panel.close()
        restore()


@test
def test_artifact_kinds_get_the_mcp_servers_they_can_choose_from(tmp: Path):
    """`/api/kinds` несёт серверы, из которых форма вида выбирает поле `mcp`: те же, что получит прогон, — с сервером
    самой Авроры (его описание пишет панель, поэтому `about` пустой и `own`). Личных серверов машины прогон не видит."""
    ck = _ck()
    (tmp / "parent").mkdir()
    project = make_project(tmp / "parent")
    rows = ck.kinds_mcp(str(project))
    assert {"name": "aurora", "own": True, "about": ""} in rows, rows
    assert all(set(r) == {"name", "own", "about"} for r in rows), rows
