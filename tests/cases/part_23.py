"""Проверки движка Aurora, часть 23: поиск Авроры для других агентов и «Установка» на
Windows (1.159.0).

Каркас и помощники — tests/harness.py. MCP-сервер в режиме `--all` — один на все проекты
машины: каждый инструмент требует `project`, базы не смешиваются, поиск тот же, что в
«Спросить». Страница «Установка» проверяет и ставит пакеты в Python панели и ищет
программы и вне PATH.
"""
from __future__ import annotations

from pathlib import Path
import os
import sys

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401


def _mcp():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib
    stdout = sys.stdout
    try:
        return importlib.import_module("aurora_mcp")
    finally:
        sys.stdout = stdout          # сервер уводит stdout в stderr — тестам он нужен свой


def _card(root: Path, name: str, text: str) -> None:
    d = root / "AuroraKnowledgeDB" / "Concepts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\ntype: concept\nstatus: knowledge\n---\n# {name}\n\n{text}\n",
                                  encoding="utf-8")


@test
def test_one_mcp_server_searches_any_project_by_its_slug(tmp: Path):
    """`aurora_mcp.py --all`: kb_projects называет базы, каждый инструмент требует project,
    чужой слаг — отказ со списком, поиск идёт в одной базе и начинается с её имени."""
    M = _mcp()
    restore = set_home(tmp / "home")
    try:
        roots = tmp / "roots"
        (roots / "a").mkdir(parents=True)
        (roots / "b").mkdir(parents=True)
        a = make_project(roots / "a")
        b = make_project(roots / "b")
        (a / "aurora.config.yaml").write_text('project:\n  name: "Альфа"\n  slug: "alpha"\n', encoding="utf-8")
        (b / "aurora.config.yaml").write_text('project:\n  name: "Бета"\n  slug: "beta"\n', encoding="utf-8")
        _card(a, "Обеспечение-ККТ", "Обеспечение кассовой техникой для пилотного проекта.")
        _card(b, "Склад", "Учёт складских остатков.")
        (tmp / "home" / ".aurora").mkdir(parents=True, exist_ok=True)
        (tmp / "home" / ".aurora" / "cockpit-roots.txt").write_text(
            f"{roots / 'a'}\n{roots / 'b'}\n", encoding="utf-8")
        assert set(M.project_map()) >= {"alpha", "beta"}, M.project_map()
        listed = M.call_tool_all("kb_projects", {})
        assert "alpha" in listed and "beta" in listed
        names = {t["name"] for t in M.TOOLS_ALL}
        assert names == {"kb_projects"} | {t["name"] for t in M.TOOLS}
        for t in M.TOOLS_ALL:
            if t["name"] != "kb_projects":
                assert "project" in t["inputSchema"]["required"], f"{t['name']} без project"
        try:
            M.call_tool_all("kb_search", {"project": "gamma", "query": "x"})
            raise AssertionError("чужой слаг не отвергнут")
        except M.ToolError as e:
            assert "alpha" in str(e) and "beta" in str(e), "отказ не называет доступные проекты"
        try:
            M.call_tool_all("kb_search", {"query": "касса"})
            raise AssertionError("поиск без project прошёл — базы смешаются")
        except M.ToolError:
            pass
        found = M.call_tool_all("kb_search", {"project": "ALPHA", "query": "обеспечение кассовой техникой"})
        assert found.startswith("Проект «alpha»") and "Обеспечение-ККТ" in found and "Склад" not in found, found
        cfg = M.client_configs()
        oc = cfg["opencode"]["mcp"]["aurora"]
        assert oc["type"] == "local" and oc["command"][-1] == "--all" and oc["command"][0] == sys.executable
        assert cfg["mcpServers"]["mcpServers"]["aurora"]["args"][-1] == "--all"
    finally:
        restore()


@test
def test_install_page_installs_into_the_panels_python(_t):
    """Пакеты — в Python панели (`python -m pip`), программы — командой своей системы
    (winget на Windows) и ищутся вне PATH; установить можно только пакет из списка."""
    sys.path.insert(0, str(KIT / "cockpit"))
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    env = ck.environment(fresh=True)
    assert env["python_path"] == sys.executable
    py = [i for i in env["items"] if i["kind"] == "py"]
    assert py and all(i["pip"] and "-m pip install " + i["pip"] in i["install"] for i in py), py
    assert all(sys.executable in i["install"] for i in py), "команда ставит не в Python панели"
    assert ck.ENV_BIN["pandoc"]["nt"].startswith("winget install") and \
        ck.ENV_BIN["git"]["nt"].startswith("winget install"), "на Windows подсказка не та"
    assert ck.env_install("requests; rm -rf /")["code"] == "unknown", "поставили не из списка"
    AC = importlib.import_module("aurora_common")
    assert AC.find_bin("git"), "git не найден"
    assert AC.find_bin("нет-такой-программы") == ""
    assert "%LOCALAPPDATA%" in " ".join(AC.BIN_PLACES["pandoc"])
    ship = (SCRIPTS / "ship_doc.py").read_text(encoding="utf-8")
    assert 'find_bin("pandoc")' in ship and 'shutil.which("pandoc")' not in ship
    view = (KIT / "cockpit/modules/install/view.js").read_text(encoding="utf-8")
    for need in ('"/api/env/install"', '"/api/env?fresh=1"', "install.python_path", "pipInstall("):
        assert need in view, need
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    assert "function auroraSearchCard(" in panel and "copyButton(text)" in panel, \
        "настройку поиска Авроры для других агентов не скопировать"
