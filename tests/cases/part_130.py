"""Проверки движка Aurora, часть 130. Каркас и помощники — tests/harness.py.

Скрытые проекты (1.190.0): панель не ведёт список проектов, а находит их по корням поиска, поэтому «убрать проект» — это
запись его пути в список скрытых на машине (`~/.aurora/cockpit-hidden.txt`); файлы проекта не трогаются, вернуть можно из
«Настройки кита». Файл списка во всех проверках подменён на временный.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test, ui_source

COCKPIT = KIT / "cockpit"


def _ck():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import aurora_cockpit as ck
    return ck


class _TmpHidden:
    """Список скрытых — во временной папке; настоящий `~/.aurora` не затрагивается."""

    def __init__(self, ck, tmp: Path):
        self.ck, self.tmp, self.path = ck, tmp, str(tmp / "home" / ".aurora" / "cockpit-hidden.txt")

    def __enter__(self):
        self.saved = self.ck.HIDDEN_FILE
        self.ck.HIDDEN_FILE = self.path
        assert os.path.realpath(self.path).startswith(os.path.realpath(str(self.tmp))), "подмена вне временной папки"
        return self

    def __exit__(self, *a):
        self.ck.HIDDEN_FILE = self.saved


def _projects(tmp: Path) -> tuple:
    root = tmp / "root"
    names = {}
    for slug, title in (("alpha", "Альфа"), ("beta", "Бета")):
        d = root / slug
        d.mkdir(parents=True)
        (d / "aurora.config.yaml").write_text(f'project:\n  name: "{title}"\n  slug: "{slug}"\n', encoding="utf-8")
        names[slug] = str(d)
    return str(root), names


@test
def test_a_hidden_project_disappears_from_the_list_and_comes_back(tmp: Path):
    """Скрытый проект панель не находит, как будто его нет; файлы на месте; возврат убирает путь из списка."""
    ck = _ck()
    root, p = _projects(tmp)
    with _TmpHidden(ck, tmp):
        known = {x["path"] for x in ck.find_projects([root])}
        assert known == set(p.values()), known
        res = ck.hidden_edit({"hide": p["alpha"]}, known)
        assert res["ok"] and [h["path"] for h in res["hidden"]] == [p["alpha"]], res
        assert res["hidden"][0]["name"] == "Альфа" and res["hidden"][0]["exists"] is True, res
        assert {x["path"] for x in ck.find_projects([root])} == {p["beta"]}, "скрытый проект остался в списке"
        assert (Path(p["alpha"]) / "aurora.config.yaml").is_file(), "скрытие тронуло файлы проекта"
        text = Path(ck.HIDDEN_FILE).read_text(encoding="utf-8")
        assert p["alpha"] in text and text.startswith("#"), text
        back = ck.hidden_edit({"show": p["alpha"], "expect": res["stamp"]}, {p["beta"]})
        assert back["ok"] and back["hidden"] == [], back
        assert {x["path"] for x in ck.find_projects([root])} == set(p.values())


@test
def test_hiding_checks_what_is_hidden_and_what_the_page_saw(tmp: Path):
    """Прятать можно только проект из списка панели, а не произвольный путь; нужен ровно один из ключей; слепок списка,
    не совпавший с файлом, — конфликт (409): страница не затрёт чужую правку."""
    ck = _ck()
    root, p = _projects(tmp)
    with _TmpHidden(ck, tmp):
        known = {x["path"] for x in ck.find_projects([root])}
        assert "нет" in ck.hidden_edit({"hide": str(tmp / "nowhere")}, known)["error"]
        assert "ровно один" in ck.hidden_edit({}, known)["error"]
        assert "ровно один" in ck.hidden_edit({"hide": p["alpha"], "show": p["beta"]}, known)["error"]
        first = ck.hidden_edit({"hide": p["alpha"], "expect": ck.hidden_state()["stamp"]}, known)
        assert first["ok"], first
        stale = ck.hidden_edit({"hide": p["beta"], "expect": "устарело"}, known)
        assert stale["conflict"] is True and [h["path"] for h in stale["hidden"]] == [p["alpha"]], stale
        assert [h["path"] for h in ck.hidden_state()["hidden"]] == [p["alpha"]], "конфликт всё равно записал"


@test
def test_the_page_offers_hiding_in_the_project_and_restoring_in_the_setup(tmp: Path):
    """Страница: кнопка в «Настройках проекта» и список «Скрытые проекты» в «Настройке кита» с кнопкой «Вернуть»; запись идёт
    со слепком списка (`expect`); подписи — на обоих языках; обработчик знает коды 200, 400 и 409."""
    ui = ui_source()
    assert "async function hideProject(" in ui and "/api/hidden" in ui and "expect: cur.stamp" in ui
    assert "renderPanelCard(box);" in ui and 'sgroup(box, "project:panel"' in ui
    assert "await drawHiddenGroup(box);" in ui and 'sgroup(box, "setup:hidden"' in ui
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    h = src[src.rindex('if u.path == "/api/hidden":'):]
    h = h[:h.index("return\n")]
    assert "409 if res.get(\"conflict\")" in h and "400 if res.get(\"error\")" in h, h
    for lang in ("ru", "en"):
        cat = json.loads((COCKPIT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for k in ("hidden.title", "hidden.about", "hidden.btn", "hidden.done", "hidden.setup_title", "hidden.setup_about",
                  "hidden.none", "hidden.restore", "hidden.restored", "hidden.missing"):
            assert cat.get(k), (lang, k)


@test
def test_the_clone_folder_is_named_by_the_person_and_checked_by_the_server(tmp: Path):
    """Проект из git клонируется в папку, которую назвал человек: путь — «папка проектов + название»; одно имя репозитория у
    разных проектов не занимает одно место. Название — одно имя, а не путь: выйти из папки проектов через него нельзя."""
    ck = _ck()
    root = str(tmp / "projects")
    got, why = ck.clone_target({"folder": "Alpha", "parent": root}, [str(tmp / "other")])
    assert why == "" and got == os.path.normpath(os.path.join(root, "Alpha")), (got, why)
    other, _ = ck.clone_target({"folder": "Beta", "parent": root}, [])
    assert other != got and os.path.basename(other) == "Beta", "две папки одного репозитория совпали"
    # папка проектов не названа — первый корень поиска
    first, why = ck.clone_target({"folder": "Gamma"}, [root, str(tmp / "x")])
    assert why == "" and first == os.path.normpath(os.path.join(root, "Gamma")), (first, why)
    # ни названия, ни прежнего пути — отказ, а не папка «по имени репозитория»
    assert ck.clone_target({}, [root])[1] and ck.clone_target({"folder": "  "}, [root])[1]
    # прежний путь (старые страницы, терминал) работает
    assert ck.clone_target({"path": str(tmp / "old" / "p")}, [])[0] == os.path.normpath(str(tmp / "old" / "p"))
    for bad in ("../x", "a/b", "a\\b", "..", ".", ".hidden", "x.", "con", "NUL.txt", "a:b", "a*b", "a|b", "q" * 101):
        assert ck.folder_name_problem(bad), f"название {bad!r} принято"
        assert ck.clone_target({"folder": bad, "parent": root}, [])[0] == "", bad
    for good in ("Northwind", "Проект-1", "a b", "v1.2"):
        assert ck.folder_name_problem(good) == "", good
    assert "корень поиска" in ck.clone_target({"folder": "A"}, [])[1], "без папки проектов и корней — понятный отказ"


@test
def test_the_git_form_asks_for_a_folder_name_and_shows_the_path(tmp: Path):
    """Форма «Из git-репозитория»: поле «Название папки» (обязательное) и «Папка проектов», итоговый путь виден сразу;
    имя репозитория — только подсказка; на сервер уходят `folder` и `parent`, а не путь по имени репозитория."""
    ui = ui_source()
    form = ui[ui.index("function newGitRest()"):ui.index("async function checkNewGit")]
    assert 'text("folder", g.folder' in form and 'text("parent", g.parent' in form and "newGitTargetText(g)" in form
    assert "g.pathTouched" not in ui and "g.path.trim()" not in ui, "прежний путь по имени репозитория остался"
    create = ui[ui.index("async function createNewGit()"):]
    create = create[:create.index("\n}\n")]
    assert "g.folder.trim()" in create and "setup.g_need_folder" in create and "folder: g.folder.trim()" in create, create
    parse = ui[ui.index("async function parseNewGit()"):ui.index("// Настройка Git проекта из разобранной строки")]
    assert "g.repoName" in parse and "g.path" not in parse, "разбор адреса снова назначает путь"
    for lang in ("ru", "en"):
        cat = json.loads((COCKPIT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for k in ("setup.g_folder", "setup.g_folder_hint", "setup.g_parent", "setup.g_parent_hint", "setup.g_target",
                  "setup.g_target_none", "setup.g_need_folder"):
            assert cat.get(k), (lang, k)
        assert "setup.g_path" not in cat, "осталась подпись прежнего поля"


@test
def test_the_update_route_has_the_same_short_tail_as_the_rebuild(tmp: Path):
    """«Обновить базу» после застоя или сбоя посреди цикла выполняет тот же краткий хвост без модели, что и «Пересобрать»:
    связи, карты, трассировка, доверие, оглавления; все шаги хвоста стоят после цикла, шагов с моделью среди них нет."""
    ck = _ck()
    routes = {s["id"]: s for s in ck.all_scenarios()}
    lite = lambda sc: [s["cmd"] for s in sc["steps"] if s.get("lite")]            # noqa: E731
    assert lite(routes["update"]) == lite(routes["rebuild"]) == ["kb:links", "kb:moc", "kb:moc", "ops:trace-table",
                                                                  "kb:trust", "kb:index"], lite(routes["update"])
    steps = [s for s in routes["update"]["steps"] if s.get("cmd") or s.get("cycle")]
    end = next(i for i, s in enumerate(steps) if s.get("cycle") == "конец цикла")
    assert all(s.get("lite") is None or i > end for i, s in enumerate(steps)), "шаг хвоста внутри цикла"
    assert not lite(routes["fix"]), "у «Починить» цикла нет — хвосту нечего заменять"
