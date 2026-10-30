"""Проверки движка Aurora, часть 174. Каркас — tests/harness.py, подставная Jira — part_170.py, сервер — part_107.py.

Заменяемые коннекторы, обвязка: API сервера панели для следующей фазы (коннекторы ролей — чтение и запись со слепком и
409; типы задач и эпики из кэша; «создать задачу для документа» — та же логика, что `ship:task --apply`), английские
причины отказа, раскладка скрипта операций в проект (`install`, `update`, список файлов движка) и проверка имени
скрипта в манифесте.
"""
from __future__ import annotations

import contextlib
import io
import json
import urllib.parse
from pathlib import Path

from cases import part_107 as S
from cases import part_170 as T
from harness import KIT, make_project, set_home, test


def _q(root: Path, **kw) -> str:
    return "?" + urllib.parse.urlencode({"project": str(root), **kw})


@test
def test_connector_roles_panel_api_reads_and_writes_with_a_snapshot_and_409(tmp: Path):
    """`GET /api/connectors/roles` — по ролям: активный коннектор, откуда выбор, из кого выбирать, слепок блока;
    причина «несколько коннекторов» по-английски в английском режиме. `POST /api/connectors/roles/save` пишет блок
    `connectors:` со слепком: устаревший или не присланный — 409 и файл не тронут; неизвестный коннектор — 400."""
    restore = set_home(tmp / "home")
    root = make_project(tmp)
    T._other_tracker(root)
    cfg = root / "aurora.config.yaml"
    srv, call = S._server(tmp)
    try:
        st, d = call("GET", "/api/connectors/roles" + _q(root))
        assert st == 200 and set(d["roles"]) == {"tasks", "wiki"} and d["digest"], d
        tasks = d["roles"]["tasks"]
        assert tasks["active"] == "" and tasks["kind"] == "ambiguous" and "несколько коннекторов" in tasks["error"], tasks
        assert {c["id"] for c in tasks["choices"]} >= {"othertrack"} and d["roles"]["wiki"]["from"] == "single", d
        st, en = call("GET", "/api/connectors/roles" + _q(root, lang="en"))
        assert "several connectors declare the role" in en["roles"]["tasks"]["error"], en["roles"]["tasks"]
        before = cfg.read_bytes()
        st, saved = call("POST", "/api/connectors/roles/save",
                         {"project": str(root), "roles": {"tasks": "othertrack"}, "expect": d["digest"]})
        assert st == 200 and saved["ok"] and saved["state"]["roles"]["tasks"]["active"] == "othertrack", saved
        assert saved["state"]["roles"]["tasks"]["from"] == "config" and saved["digest"] != d["digest"], saved
        after = cfg.read_bytes()
        assert after.startswith(before) and after.endswith(b"  tasks: othertrack\n"), after[-200:]
        for body in ({"project": str(root), "roles": {"tasks": ""}, "expect": d["digest"]},
                     {"project": str(root), "roles": {"tasks": ""}}):
            st, stale = call("POST", "/api/connectors/roles/save", body)
            assert st == 409 and stale["conflict"] and cfg.read_bytes() == after, (st, stale)
        st, bad = call("POST", "/api/connectors/roles/save",
                       {"project": str(root), "roles": {"tasks": "no-such"}, "expect": saved["digest"]})
        assert st == 400 and bad["code"] == "unknown_connector", bad
        st, nope = call("GET", "/api/connectors/roles" + _q(tmp / "elsewhere"))
        assert "roles" not in nope, "чужой путь прочитан как проект"
    finally:
        srv.shutdown()
        restore()


@test
def test_task_options_and_create_panel_api_use_the_same_logic_as_ship_task(tmp: Path):
    """`GET /api/task/options` — типы задач и эпики проекта трекера (ключ проекта — из настроек коннектора), второй раз —
    из кэша, `fresh=1` — заново; отказ — причина ядра (по-английски в английском режиме) и текст коннектора.
    `POST /api/task/create` — то же, что `ship:task --apply`: задача, штамп, повтор без второй задачи."""
    restore = set_home(tmp / "home")
    tracker = T.Tracker()
    root = T.jira_project(tmp, tracker.url)
    doc = root / "Artifacts" / "us" / "US-3.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: us\n---\n\n# Отчёт за квартал\n", encoding="utf-8")
    srv, call = S._server(tmp)
    try:
        with T.env(**T.jira_env(tmp)):
            st, d = call("GET", "/api/task/options" + _q(root))
            assert st == 200 and d["ok"] and d["project_key"] == "PRJA" and not d["cached"], d
            assert [t["name"] for t in d["types"]] == ["Story", "Epic", "Task"], d
            assert [e["key"] for e in d["epics"]] == ["PRJA-2", "PRJA-3", "PRJA-1"], d
            st, again = call("GET", "/api/task/options" + _q(root))
            assert again["cached"] and again["types"] == d["types"], again
            st, fresh = call("GET", "/api/task/options" + _q(root, fresh="1"))
            assert not fresh["cached"], fresh
            st, miss = call("GET", "/api/task/options" + _q(root, project_key="PRJZ", lang="en"))
            assert not miss["ok"] and miss["code"] == "not_found" and "404" in miss["detail"], miss
            assert miss["error"].startswith("the connector «jira-dc»: the server did not find"), miss["error"]
            st, res = call("POST", "/api/task/create", {"project": str(root), "path": "Artifacts/us/US-3.md",
                                                       "task": {"type": "Task", "epic": "PRJA-2"}})
            assert st == 200 and res["outcome"] == "ok" and res["key"] == "PRJA-5" and "lines" not in res, res
            assert "task_key: PRJA-5\n" in doc.read_text(encoding="utf-8")
            f = tracker.created()[0]["fields"]
            assert f["issuetype"] == {"name": "Task"} and f["summary"] == "Отчёт за квартал" and "assignee" not in f, f
            st, res = call("POST", "/api/task/create", {"project": str(root), "path": "Artifacts/us/US-3.md",
                                                       "task": {"type": "Task"}})
            assert st == 200 and res["outcome"] == "exists" and len(tracker.created()) == 1, res
            st, res = call("POST", "/api/task/create", {"project": str(root), "path": "Artifacts/us/none.md"})
            assert st == 400 and res["code"] == "no_file", res
    finally:
        srv.shutdown()
        tracker.close()
        restore()


@test
def test_install_and_update_lay_out_connector_operations_scripts(tmp: Path):
    """Скрипт операций коннектора едет в проект вместе со скриптом выгрузки: `install` кладёт его в `.aurora/scripts/`,
    `update` — тоже, и он в списке файлов движка (по нему doctor называет неполную копию)."""
    T._mod("aurora_common")
    import aurora_update as AU
    import install_aurora as IA
    targets = AU.engine_targets()
    for name in ("jira_ops.py", "confluence_ops.py", "connector_ops.py", "task_doc.py"):
        assert f".aurora/scripts/{name}" in targets, (name, [t for t in targets if t.endswith("_ops.py")])
    target = tmp / "installed"
    target.mkdir()
    inst = IA.Installer(target, "Demo", "Demo", None, None, force=False, dry_run=False)
    with contextlib.redirect_stdout(io.StringIO()):
        inst.install_connectors()
    for cid, script in (("jira-dc", "jira_ops.py"), ("confluence-dc", "confluence_ops.py")):
        assert (target / ".aurora/scripts" / script).read_bytes() == (KIT / "connectors" / cid / script).read_bytes()
    root = make_project(tmp)
    for script in ("jira_ops.py", "confluence_ops.py"):          # проект до обновления: скриптов операций ещё нет
        (root / ".aurora/scripts" / script).unlink()
    plan = {c.path: c for c in AU.plan(root, AU.project_fields(root))}
    for cid, script in (("jira-dc", "jira_ops.py"), ("confluence-dc", "confluence_ops.py")):
        change = plan.get(f".aurora/scripts/{script}")
        assert change and change.kind == "write", (script, sorted(p for p in plan if p.startswith(".aurora/scripts/")))
        assert change.new_text == (KIT / "connectors" / cid / script).read_text(encoding="utf-8")


@test
def test_connector_manifest_operations_script_must_be_a_plain_name(tmp: Path):
    """`operations.script` манифеста становится именем файла в `.aurora/scripts/` — как `run.script`, только простое
    имя: с «/» или «..» модуль пропускается с причиной; поле `operations` не того вида — тоже пропуск, а не догадка."""
    import aurora_common as C
    base = json.loads((KIT / "connectors/jira-dc/connector.json").read_text(encoding="utf-8"))
    ok, rep = C.load_connector_manifest(_dump(tmp / "ok.json", base))
    assert ok and ok["operations"]["script"] == "jira_ops.py" and not rep, rep
    assert C.connector_scripts(ok) == ["jira_export.py", "jira_ops.py"]
    for bad in ("../x.py", "a/b.py", "..\\x.py"):
        got, rep = C.load_connector_manifest(_dump(tmp / "b.json", {**base, "operations": {**base["operations"],
                                                                                            "script": bad}}))
        assert got is None and "operations.script" in rep[0], (bad, rep)
    got, rep = C.load_connector_manifest(_dump(tmp / "c.json", {**base, "operations": ["issue_types"]}))
    assert got is None and "operations" in rep[0], rep
    got, rep = C.load_connector_manifest(_dump(tmp / "d.json", {**base, "operations": {**base["operations"], "ops": "x"}}))
    assert got is None and "operations.ops" in rep[0], rep


def _dump(path: Path, data) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@test
def test_connector_and_task_causes_have_english_and_the_same_holes(_t):
    """Причины отказа операций и `ship:task` — таблицы `CAUSES` с русским и английским текстом и одинаковыми
    подстановками: панель переводит сообщения по ним, `kit:i18n --check` их сверяет."""
    import re
    KI = T._mod("kit_i18n")
    assert not [p for p in KI.cause_problems() if "connector_ops" in p or "task_doc" in p], KI.cause_problems()
    for name in ("connector_ops", "task_doc"):
        for cid, row in T._mod(name).CAUSES.items():
            holes = lambda t: sorted(re.findall(r"\{(\w+)\}", t))  # noqa: E731
            assert row["en"].strip() and holes(row["ru"]) == holes(row["en"]), (name, cid)
            assert not re.search("[а-яА-ЯёЁ]", re.sub(r"«[^»]*»", "", row["en"])), (name, cid, row["en"])
    ck = S._ck()
    ck._MATCHERS.clear()
    CO = T._mod("connector_ops")
    assert ck.translate_message(CO.why("net", id="jira-dc", op="epics"), "en") == \
        "the connector «jira-dc»: the server did not answer — check the network or the VPN"


@test
def test_connectors_block_follows_the_line_ending_of_the_line_it_joins(_t):
    """Конфиг со смешанными окончаниями строк (с Windows, дописан на маке): новый блок `connectors:` продолжает окончание
    последней строки, блок, который уже есть, сохраняет свои, — без `\\n\\r\\n` на стыке. Нашёл CI на Windows."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import connector_ops as C
    mixed = 'a: 1\r\nb: 2\r\nothertrack:\n  url: "https://tracker.example.org"\n'
    out = C.replace(mixed, {"tasks": "othertrack"})
    assert out.startswith(mixed) and out.endswith("  tasks: othertrack\n") and "\n\r\n" not in out, repr(out[-120:])
    crlf = C.replace("a: 1\r\n", {"tasks": "t"})
    assert crlf.endswith("  tasks: t\r\n") and "\r\n\r\nconnectors:" in crlf, repr(crlf)
    again = C.replace(crlf, {"tasks": "u"})
    assert again.endswith("  tasks: u\r\n") and again.count("connectors:") == 1, repr(again)
    assert "connectors" not in C.replace(again, {}), "пустой выбор не убрал блок"
