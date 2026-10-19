"""Проверки движка Aurora, часть 90. Каркас и помощники — tests/harness.py.

Слой ролей сервисов (BL-DEC-06): ядро ищет зеркало трекера задач и вики по РОЛИ из манифеста
коннектора (`tasktracker`, `projectwiki`, …), а не по названию продукта; старые имена остаются
псевдонимами, а проекты и копии манифестов, написанные до слоя, работают как работали.
Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _with_sources(root: Path, entries: list) -> None:
    """Дописать в конфиг проекта секцию `sources:` (id, module, path на запись)."""
    cfg = root / "aurora.config.yaml"
    lines = ["sources:"]
    for eid, module, path in entries:
        lines += [f"  - id: {eid}", f"    module: {module}", f"    path: {path}"]
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n".join(lines) + "\n", encoding="utf-8")


def _task(path: Path, key: str, status: str, **fm) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "".join(f'{k}: "{v}"\n' for k, v in fm.items())
    path.write_text(f'---\nkey: "{key}"\ntitle: "Задача {key}"\nstatus: "{status}"\n{extra}---\n\nТ.\n',
                    encoding="utf-8")


@test
def test_service_role_is_resolved_from_manifest_and_old_names(tmp: Path):
    """Роль сервиса берётся из манифеста; роль, имя модуля и старое имя продукта — одно и то же."""
    import sources_registry as R
    root = make_project(tmp)
    for name, want in (("tasktracker", "tasktracker"), ("Jira", "tasktracker"), ("JIRA", "tasktracker"),
                       ("jira-dc", "tasktracker"), ("confluence", "projectwiki"),
                       ("projectwiki", "projectwiki"), ("confluence-dc", "projectwiki"),
                       ("web", "webpages"), ("webpages", "webpages")):
        assert R.canonical_role(name, str(root)) == want, (name, R.canonical_role(name, str(root)))
    # неизвестное имя и пустое не превращаются в чужую роль
    for name in ("", None, "git", "documents-of-nobody"):
        assert R.canonical_role(name, str(root)) in ("",), name
    # словарь ролей закрытый и в нём нет названий продуктов
    assert set(R.SERVICE_ROLES) == {"tasktracker", "projectwiki", "documents", "coderepo", "testmgmt", "webpages"}
    inst = {i["module"]: i["service_role"] for i in R.instances(str(root))}
    assert inst == {"jira-dc": "tasktracker", "confluence-dc": "projectwiki"}, inst
    assert R.service_paths(str(root), "tasktracker") == ["Sources/JIRA"]
    assert R.service_path(str(root), "Confluence") == "Sources/Confluence"
    assert R.service_path(str(root), "coderepo") == "", "роли без коннектора не должно быть источника"


@test
def test_service_dirs_follow_the_declared_mirror_path_not_the_product_name(tmp: Path):
    """Зеркало трекера в другой папке: доверие читает ЕЁ, а не `Sources/<продукт>` (на старом коде — пусто)."""
    import aurora_common as C
    import kb_trust as T
    root = make_project(tmp)
    _with_sources(root, [("Tasks", "jira-dc", "Sources/Tasks"), ("Wiki", "confluence-dc", "Sources/Wiki")])
    _task(root / "Sources" / "Tasks" / "AB-1.md", "AB-1", "Done", type="Story")
    _task(root / "Sources" / "Tasks" / "AB-2.md", "AB-2", "In Progress", parent="AB-1")
    # старая папка с чужой задачей не должна подмешиваться: проект её отключил
    _task(root / "Sources" / "JIRA" / "OLD-1.md", "OLD-1", "Done")
    assert C.service_dirs("tasktracker", str(root)) == ["Sources/Tasks"]
    assert C.service_dirs("projectwiki", str(root)) == ["Sources/Wiki"]
    assert T.task_status(str(root)) == {"AB-1": "Done", "AB-2": "In Progress"}, T.task_status(str(root))
    assert T.task_parents(str(root)) == {"AB-2": "AB-1"}
    assert T.task_types(str(root)) == {"AB-1": "Story", "AB-2": ""}
    # вики: корень с галочкой «доверять» ищется в её зеркале и называется её путём
    page = root / "Sources" / "Wiki" / "Корень"
    page.mkdir(parents=True)
    (page / "index.md").write_text('---\npage_id: 555\ntitle: "Корень"\n---\n\nТ.\n', encoding="utf-8")
    assert T.trusted_roots(str(root), [{"page_id": "555", "title": "К", "trusted": True}]) \
        == [("Sources/Wiki/Корень/", "К")]
    T.PROJECT_ROOT[0] = str(root)
    try:
        assert T.wiki_dirs() == ["Sources/Wiki/"]
        assert T.wiki_of("Sources/Wiki/Корень/index.md") == "Sources/Wiki/"
        assert T.wiki_of("Sources/Confluence/Корень/index.md") == ""
    finally:
        T.PROJECT_ROOT[0] = "."


@test
def test_disconnected_service_keeps_its_default_mirror_for_trust(tmp: Path):
    """Коннектор снят из `sources:`, а зеркало осталось на диске: доверие не пропадает вместе со строкой конфига."""
    import sources_registry as R
    import kb_trust as T
    root = make_project(tmp)
    _with_sources(root, [("Wiki", "confluence-dc", "Sources/Confluence")])      # трекера в секции нет
    _task(root / "Sources" / "JIRA" / "AB-3.md", "AB-3", "Done")
    assert R.service_instances(str(root), "tasktracker") == [], "трекер не подключён"
    assert R.service_paths(str(root), "tasktracker") == ["Sources/JIRA"]
    assert T.task_status(str(root)) == {"AB-3": "Done"}


@test
def test_stale_project_manifest_copy_gets_service_role_from_the_kit(tmp: Path):
    """Копия манифеста в проекте старше поля `service_role` (update ещё не прошёл) — роль берётся у кита."""
    import sources_registry as R
    root = make_project(tmp)
    copy = root / ".aurora" / "connectors" / "jira-dc.json"
    m = json.loads(copy.read_text(encoding="utf-8"))
    for k in ("service_role", "aliases"):
        m.pop(k, None)
    copy.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    assert R.load(str(copy))["service_role"] == "tasktracker"
    assert R.canonical_role("jira", str(root)) == "tasktracker"
    # копия чужого модуля без роли кита не получает: ей нечего наследовать
    other = dict(m, id="own-tracker")
    own = root / ".aurora" / "connectors" / "own-tracker.json"
    own.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
    assert R.load(str(own))["service_role"] == ""


@test
def test_project_copy_cannot_reassign_the_role_of_a_kit_module(tmp: Path):
    """Копия манифеста модуля кита с чужой ролью сервиса: решает кит; свой коннектор (нет в ките) несёт роль сам."""
    import sources_registry as R
    root = make_project(tmp)
    copy = root / ".aurora" / "connectors" / "jira-dc.json"
    m = json.loads(copy.read_text(encoding="utf-8"))
    m["service_role"], m["aliases"] = "projectwiki", ["confluence"]
    copy.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    got = R.load(str(copy))
    assert got["service_role"] == "tasktracker" and "confluence" not in got["aliases"], got
    assert R.service_paths(str(root), "projectwiki") == ["Sources/Confluence"], R.service_paths(str(root), "projectwiki")
    own = root / ".aurora" / "connectors" / "own-wiki.json"
    own.write_text(json.dumps(dict(m, id="own-wiki", service_role="projectwiki", aliases=[]), ensure_ascii=False), encoding="utf-8")
    assert R.load(str(own))["service_role"] == "projectwiki"


@test
def test_a_failed_registry_read_is_not_remembered_as_an_answer(tmp: Path):
    """Сбой чтения манифеста (замок файла на Windows) даёт прежние папки на этот раз, но не запоминается: следующий вопрос
    снова идёт в реестр. Запоминается только слепой реестр (манифестов нет совсем)."""
    import aurora_common as C
    import sources_registry as R
    root = make_project(tmp)
    real, calls = R.service_paths, []

    def flaky(root_, role):
        calls.append(role)
        if len(calls) == 1:
            raise PermissionError("манифест занят")
        return real(root_, role)
    R.service_paths = flaky
    C._SERVICE_MEMO.clear()
    try:
        assert C.service_dirs("tasktracker", str(root)) == ["Sources/JIRA"]       # прежняя папка на время сбоя
        assert C.service_dirs("tasktracker", str(root)) == ["Sources/JIRA"]
        assert len(calls) == 2, "сбой чтения запомнен как ответ реестра"
    finally:
        R.service_paths = real
        C._SERVICE_MEMO.clear()


@test
def test_foreign_manifest_cannot_take_a_role_it_does_not_have(tmp: Path):
    """Манифест со стороны: неизвестная роль не роняет модуль, чужое старое имя не перехватывает роль."""
    import sources_registry as R
    root = make_project(tmp)
    conn = root / ".aurora" / "connectors"
    base = json.loads((conn / "jira-dc.json").read_text(encoding="utf-8"))
    odd = dict(base, id="odd-tracker", service_role="gods-eye", aliases=["jira", "ok_name", "bad name!", 7, "x" * 80],
               mirror={"default_path": "Sources/Odd"}, run=dict(base["run"], command="sync:odd"))
    (conn / "odd-tracker.json").write_text(json.dumps(odd, ensure_ascii=False), encoding="utf-8")
    got = R.load(str(conn / "odd-tracker.json"))
    assert got and got["service_role"] == "", "неизвестная роль должна отбрасываться, а модуль — работать"
    assert got["aliases"] == ["jira", "ok_name"], got["aliases"]
    assert any("gods-eye" in line for line in R.registry_report()), R.registry_report()
    # чужой модуль без роли имя «jira» не занимает: роль по-прежнему одна и верная
    assert R.canonical_role("jira", str(root)) == "tasktracker"
    # а два модуля с РАЗНЫМИ ролями, делящие одно имя, — не гадаем
    wiki_like = dict(odd, id="odd-wiki", service_role="projectwiki", aliases=["jira"],
                     mirror={"default_path": "Sources/OddWiki"}, run=dict(base["run"], command="sync:oddwiki"))
    (conn / "odd-wiki.json").write_text(json.dumps(wiki_like, ensure_ascii=False), encoding="utf-8")
    assert R.canonical_role("jira", str(root)) == "", "спор двух ролей за одно имя решается отказом"
    assert R.canonical_role("jira-dc", str(root)) == "tasktracker"


@test
def test_old_project_without_registry_section_still_reads_historical_mirrors(tmp: Path):
    """Проект без `sources:` и зеркала с историческими именами папок работают как до слоя ролей."""
    import kb_trust as T
    import kb_trace_table as TT
    root = make_project(tmp)
    assert "sources:" not in (root / "aurora.config.yaml").read_text(encoding="utf-8")
    _task(root / "Sources" / "JIRA" / "AB-9.md", "AB-9", "Done")
    assert T.task_status(str(root)) == {"AB-9": "Done"}
    got = TT.mirrors(str(root))
    assert got.get("Sources/JIRA") == "tasks" and got.get("Sources/Confluence") == "artifacts", got


# ------------------------------------------------------------------ копия движка в проекте, сбои реестра, запоминание

def _in_project(root: Path, code: str) -> subprocess.CompletedProcess:
    """Код под `.aurora/scripts` проекта — так работают потребители в живом проекте."""
    prelude = "import sys, json; sys.path.insert(0, '.aurora/scripts'); "
    return subprocess.run([sys.executable, "-c", prelude + code], cwd=str(root), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)


def _strip_service_fields(root: Path) -> None:
    for copy in (root / ".aurora" / "connectors").glob("*.json"):
        m = json.loads(copy.read_text(encoding="utf-8"))
        for k in ("service_role", "aliases"):
            m.pop(k, None)
        copy.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")


_DIRS = ("import aurora_common as C; "
         "print(json.dumps([C.service_dirs('tasktracker'), C.service_dirs('projectwiki')]))")


@test
def test_engine_copy_in_project_with_old_manifests_still_finds_the_mirrors(tmp: Path):
    """Копия движка в `.aurora/scripts` и копии манифестов без `service_role`: роль берётся у КИТА.

    Раньше «кит» там определялся как сама папка движка (рядом с ней лежит `connectors/` с копиями) —
    ответ был пустым, и вся база молча теряла доверие по задачам."""
    root = make_project(tmp)
    _strip_service_fields(root)
    (root / ".aurora" / "kit_path.txt").write_text(str(KIT), encoding="utf-8")
    cp = _in_project(root, _DIRS)
    assert cp.returncode == 0, cp.stderr
    assert json.loads(cp.stdout) == [["Sources/JIRA"], ["Sources/Confluence"]], cp.stdout
    # указатель на кит не читается как UTF-8 — не traceback и не пустота: кит не виден, копии манифестов старше поля
    # роли не знают — слой слеп, и ответ прежний (на этом шаге раньше получалось `[[], []]`)
    (root / ".aurora" / "kit_path.txt").write_bytes(b"\xff\xfe\x00bad")
    cp = _in_project(root, _DIRS)
    assert cp.returncode == 0, cp.stderr
    assert json.loads(cp.stdout) == [["Sources/JIRA"], ["Sources/Confluence"]], (cp.stdout, cp.stderr)
    # копии свежие (роли есть), кита не видно, в `sources:` только вики: зеркало трекера, лежащее на диске, не теряется
    (tmp / "second").mkdir()
    root2 = make_project(tmp / "second")
    _with_sources(root2, [("Wiki", "confluence-dc", "Sources/Confluence")])
    cp = _in_project(root2, _DIRS)
    assert json.loads(cp.stdout) == [["Sources/JIRA"], ["Sources/Confluence"]], (cp.stdout, cp.stderr)


@test
def test_registry_blind_project_falls_back_to_historical_folders(tmp: Path):
    """Реестр не дал манифестов (пустая `.aurora/connectors/`, кита не видно): прежние папки, а не пусто."""
    root = make_project(tmp)
    for copy in (root / ".aurora" / "connectors").glob("*.json"):
        copy.unlink()
    cp = _in_project(root, _DIRS)         # указателя на кит нет, манифестов нет
    assert cp.returncode == 0, cp.stderr
    assert json.loads(cp.stdout) == [["Sources/JIRA"], ["Sources/Confluence"]], (cp.stdout, cp.stderr)
    assert "service_dirs" in cp.stderr and "прежн" in cp.stderr, cp.stderr
    # слепой реестр отвечает одно и то же, пока конфиг и манифесты те же: реестр спрашивается один раз, а не на каждую карточку
    cp = _in_project(root, "import sources_registry as R\nn = [0]\nreal = R.service_paths\n"
                           "def spy(*a, **k):\n    n[0] += 1\n    return real(*a, **k)\n"
                           "R.service_paths = spy\nimport aurora_common as C\n"
                           "for _ in range(50):\n    C.service_dirs('tasktracker')\nprint(n[0])")
    assert cp.returncode == 0 and cp.stdout.strip() == "1", (cp.stdout, cp.stderr)
    # с указателем на кит те же папки берутся из его манифестов — и без предупреждения
    (root / ".aurora" / "kit_path.txt").write_text(str(KIT), encoding="utf-8")
    cp = _in_project(root, _DIRS)
    assert json.loads(cp.stdout) == [["Sources/JIRA"], ["Sources/Confluence"]] and not cp.stderr.strip(), cp.stderr


@test
def test_service_dirs_survive_a_registry_that_raises(tmp: Path):
    """Любой сбой реестра — прежняя папка и строка в stderr, а не падение `kb_lint`, `kb_trust`, `ctx_pack`."""
    import aurora_common as C
    import sources_registry as R
    root = make_project(tmp)
    real = R.service_paths
    err = io.StringIO()

    def boom(*_a, **_kw):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "kit_path.txt не в UTF-8")
    R.service_paths = boom
    C._SERVICE_WARNED.clear()
    try:
        with contextlib.redirect_stderr(err):
            assert C.service_dirs("tasktracker", str(root)) == ["Sources/JIRA"]
            assert C.service_dirs("projectwiki", str(root)) == ["Sources/Confluence"]
            assert C.service_dirs("coderepo", str(root)) == [], "у роли без прежней папки её и не выдумывают"
    finally:
        R.service_paths = real
        C._SERVICE_WARNED.clear()
    assert "UnicodeDecodeError" in err.getvalue(), err.getvalue()


@test
def test_service_dirs_memo_sees_in_place_edits_and_cleans_config_paths(tmp: Path):
    """Запоминание не переживает правку конфига и копии манифеста на месте; пути из конфига приводятся к чистым."""
    import aurora_common as C
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    assert C.service_dirs("tasktracker", str(root)) == ["Sources/JIRA"]
    _with_sources(root, [("A", "jira-dc", "./Sources/Tasks/")])
    assert C.service_dirs("tasktracker", str(root)) == ["Sources/Tasks"], "пути `./x/` приводятся к `x`"
    cfg.write_text(base + "sources:\n  - id: B\n    module: jira-dc\n    path: Sources\\Other\n", encoding="utf-8")
    assert C.service_dirs("tasktracker", str(root)) == ["Sources/Other"]
    cfg.write_text(base + "sources:\n  - id: C\n    module: jira-dc\n    path: .\n", encoding="utf-8")
    assert C.service_dirs("tasktracker", str(root)) == [], "путь «.» — не папка зеркала"
    # копия манифеста правится на месте, каталог не меняется: ответ берётся из файла, а не из памяти
    cfg.write_text(base + "sources:\n  - id: D\n    module: jira-dc\n", encoding="utf-8")
    assert C.service_dirs("tasktracker", str(root)) == ["Sources/JIRA"]
    copy = root / ".aurora" / "connectors" / "jira-dc.json"
    m = json.loads(copy.read_text(encoding="utf-8"))
    m["mirror"]["default_path"] = "Sources/Moved"
    with open(copy, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    assert C.service_dirs("tasktracker", str(root)) == ["Sources/Moved"], C.service_dirs("tasktracker", str(root))


@test
def test_service_role_of_wrong_type_does_not_drop_the_module(tmp: Path):
    """`service_role: 5` и `aliases: "x"` — поле отбрасывается, модуль работает (строка в отчёте реестра)."""
    import sources_registry as R
    root = make_project(tmp)
    conn = root / ".aurora" / "connectors"
    base = json.loads((conn / "jira-dc.json").read_text(encoding="utf-8"))
    own = dict(base, id="own-tracker", service_role=5, aliases="jira", mirror={"default_path": "Sources/Own"},
               run=dict(base["run"], command="sync:own"))
    (conn / "own-tracker.json").write_text(json.dumps(own, ensure_ascii=False), encoding="utf-8")
    got = R.load(str(conn / "own-tracker.json"))
    assert got and got["id"] == "own-tracker" and got["service_role"] == "" and got["aliases"] == [], got
    assert any("own-tracker" in line and "«5»" in line for line in R.registry_report()), R.registry_report()
    # перевод строки в конце псевдонима — не простое слово
    assert not R._ALIAS_OK.fullmatch("jira\n") and R._ALIAS_OK.fullmatch("jira-dc")


def _custom_project(tmp: Path) -> Path:
    """Проект, где трекер и вики подключены в нестандартные папки."""
    root = make_project(tmp)
    _with_sources(root, [("Tasks", "jira-dc", "Sources/Tasks"), ("Wiki", "confluence-dc", "Sources/Wiki")])
    return root


@test
def test_linter_and_pack_read_the_tracker_mirror_by_role(tmp: Path):
    """`artifact_kind` и сводка задач в паке смотрят в зеркало трекера из `sources:`, а не в `Sources/<продукт>`."""
    import ctx_pack as CP
    import kb_lint as L
    root = _custom_project(tmp)
    _task(root / "Sources" / "Tasks" / "AB-5.md", "AB-5", "Бэклог")
    here = os.getcwd()
    os.chdir(root)
    try:
        assert L.artifact_kind("Разработка-таблицы-Х", "Разработка таблицы Х", "Sources/Tasks/AB-5.md",
                               "Concepts", None) == "задача Jira"
        assert L.artifact_kind("Разработка-таблицы-Х", "Разработка таблицы Х", "Sources/JIRA/AB-5.md",
                               "Concepts", None) == "", "старая папка трекером больше не считается"
        assert L.artifact_kind("Разработка-таблицы-Х", "Разработка таблицы Х", "Sources/Wiki/x.md",
                               "Concepts", None) == ""
        rows = CP.jira_state("статус AB-5")
        assert [(r["key"], r["status"]) for r in rows] == [("AB-5", "Бэклог")], rows
    finally:
        os.chdir(here)
    # вызывающий, который знает проект, но стоит в другой папке (панель), называет зеркало трекера сам
    import agent_runner as AR
    assert os.path.abspath(os.getcwd()) != os.path.abspath(root)
    assert L.artifact_kind("Разработка-таблицы-Х", "Разработка таблицы Х", "Sources/Tasks/AB-5.md", "Concepts",
                           None, ("Sources/Tasks/",)) == "задача Jira"
    kb = root / "AuroraKnowledgeDB" / "Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "Разработка-таблицы-Х.md").write_text(
        '---\ntitle: "Разработка таблицы Х"\ntype: concept\nkind: knowledge\nstatus: draft\n'
        'sources:\n  - "Sources/Tasks/AB-5.md"\n---\n\n# Разработка таблицы Х\n\nТ.\n', encoding="utf-8")
    # процесс стоит не в проекте (так работает панель): зеркало трекера берётся по переданному корню, а не по cwd
    assert [Path(p).name for p in AR.jira_task_cards(str(root))] == ["Разработка-таблицы-Х.md"]


@test
def test_fix_graph_and_remap_follow_the_mirrors_by_role(tmp: Path):
    """kb_fix --drop-jira, kb_graph и kb_remap берут папки зеркал из реестра, а не из названий продуктов."""
    root = _custom_project(tmp)
    _task(root / "Sources" / "Tasks" / "AB-7.md", "AB-7", "Done")
    kb = root / "AuroraKnowledgeDB" / "Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "Из-задачи.md").write_text('---\ntitle: "Из задачи"\ntype: concept\nstatus: draft\n'
                                     'sources:\n  - "Sources/Tasks/AB-7.md"\n---\n\n# Из задачи\n\nТ.\n',
                                     encoding="utf-8")
    (kb / "Из-вики.md").write_text('---\ntitle: "Из вики"\ntype: concept\nstatus: draft\n'
                                   'sources:\n  - "Sources/Wiki/p.md"\n---\n\n# Из вики\n\nТ.\n', encoding="utf-8")
    (root / "Sources" / "Wiki").mkdir(parents=True, exist_ok=True)
    (root / "Sources" / "Wiki" / "p.md").write_text('---\npage_id: 1\ntitle: "П"\n---\n\nТ.\n', encoding="utf-8")
    fix = run("kb_fix.py", "--drop-jira", cwd=root)
    assert "Карточки из задач Jira: 1 в архив" in fix.stdout and "Из-задачи" in fix.stdout, fix.stdout + fix.stderr
    # граф дошёл до зеркала вики из реестра, а не остановился на «нет Sources/Confluence/»
    g = run("kb_graph.py", cwd=root)
    assert "запускайте из корня проекта" not in g.stderr, g.stderr
    # снимок карты страниц пишется по зеркалу вики проекта
    snap = run("kb_remap.py", "--snapshot", cwd=root)
    assert snap.returncode == 0, snap.stdout + snap.stderr
    saved = [p for p in (root / "AuroraKnowledgeDB" / "meta").rglob("*.json")
             if "Sources/Wiki" in p.read_text(encoding="utf-8").replace("\\/", "/")]
    assert saved, "снимок не про зеркало вики проекта"


@test
def test_portable_names_check_groups_wiki_mirror_files_by_role(tmp: Path):
    """doctor и kb_names: файл с плохим именем в зеркале вики из реестра — «зеркало», а не «прочее»."""
    if os.name == "nt":
        return          # Windows сама отрезает точку на конце имени: плохого имени там не создать
    root = make_project(tmp, git=True)
    _with_sources(root, [("Wiki", "confluence-dc", "Sources/Wiki")])
    bad = root / "Sources" / "Wiki" / "Страница."
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("x", encoding="utf-8")
    other = root / "Misc" / "Файл."
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    cp = _in_project(root, "import aurora_doctor as D; print(json.dumps(D.check_portable_names(), ensure_ascii=False))")
    assert cp.returncode == 0, cp.stderr
    warns = json.loads(cp.stdout)
    assert any("зеркало Confluence" in w for w in warns), warns
    names = run("kb_names.py", cwd=root)
    human = names.stdout.split("Файлы человека", 1)[-1] if "Файлы человека" in names.stdout else ""
    assert "Файл." in human and "Страница." not in human, names.stdout + names.stderr
