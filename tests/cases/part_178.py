"""Проверки движка Aurora, часть 178. Каркас — tests/harness.py; подставные Confluence — part_148.py, Jira — part_170.py.

Публикация на операциях роли `wiki` (1.200.0): `ship:publish` больше не держит REST-клиента Confluence — вики проекта
это активный коннектор роли, и публикация зовёт только его операции (`site`, `find_page`, `get_page`, `children`,
`create_page`, `update_page`). Главное требование владельца для вики — подставная «другая вики» с тем же контрактом
публикует документ без правки ядра: создание, повтор без изменений, правка, чужая правка — конфликт.

Здесь же — ссылка на страницу в задаче документа после публикации (`task_key` в шапке, `link_page` активного трекера):
повтор второй ссылки не заводит, трекера нет — одна строка, публикация не страдает.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cases import part_148 as W
from cases import part_170 as T
from harness import KIT, SCRIPTS, make_project, test

OTHER_WIKI = '''import json
import os
import sys
from sources_core import OpFailure, serve_ops

STORE = os.path.join(".aurora", "state", "otherwiki.json")
BASE = "https://wiki.example.org"


def _load(project):
    with open(os.path.join(project, STORE), encoding="utf-8") as f:
        return json.load(f)


def _save(project, data):
    with open(os.path.join(project, STORE), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _view(data, p, body=True):
    chain, cur = [], p["parent"]
    while cur:
        chain.insert(0, {"id": cur, "title": data["pages"][cur]["title"]})
        cur = data["pages"][cur]["parent"]
    out = {"id": p["id"], "title": p["title"], "space": p["space"], "version": p["version"], "by": p["by"],
           "when": "2026-10-01", "ancestors": chain, "url": BASE + "/pages/viewpage.action?pageId=" + p["id"]}
    if body:
        out["body"] = p["body"]
    return out


def site(params, project):
    return {"server": BASE, "space": "DOC", "parent": "", "auth": "PAT"}


def find_page(params, project):
    data = _load(project)
    hit = next((p for p in data["pages"].values() if p["title"] == params.get("title")
                and p["space"] == params.get("space")), None)
    return {"page": _view(data, hit) if hit else None}


def get_page(params, project):
    data = _load(project)
    p = data["pages"].get(str(params.get("page_id")))
    if not p:
        raise OpFailure("not_found", "no such page")
    return _view(data, p, params.get("body", True) is not False)


def children(params, project):
    data = _load(project)
    kids = [_view(data, p, False) for p in data["pages"].values() if p["parent"] == str(params.get("page_id"))]
    return {"pages": kids, "complete": True, "gap": "", "got": len(kids)}


def create_page(params, project):
    data = _load(project)
    if any(p["title"] == params["title"] and p["space"] == params["space"] for p in data["pages"].values()):
        raise OpFailure("bad_request", "title taken")
    pid = str(len(data["pages"]) + 1)
    data["pages"][pid] = {"id": pid, "title": params["title"], "space": params["space"], "by": "Aurora",
                          "parent": str(params.get("parent_id") or "") or None, "version": 1, "body": params["body"]}
    data["log"].append(["create", pid])
    _save(project, data)
    return _view(data, data["pages"][pid])


def update_page(params, project):
    data = _load(project)
    p = data["pages"][str(params["page_id"])]
    if int(params["version"]) != p["version"]:
        raise OpFailure("conflict", "version moved")
    p.update(title=params["title"], body=params["body"], version=p["version"] + 1, by="Aurora")
    if params.get("parent_id"):
        p["parent"] = str(params["parent_id"])
    data["log"].append(["update", p["id"]])
    _save(project, data)
    return _view(data, p)


OPS = {"site": site, "find_page": find_page, "get_page": get_page, "children": children,
       "create_page": create_page, "update_page": update_page}
if __name__ == "__main__":
    sys.exit(serve_ops(OPS))
'''

KIND = ('artifacts:\n'
        '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
        '    publish_url: "https://wiki.example.org/pages/viewpage.action?pageId=1"\n')


def _other_wiki(root: Path) -> Path:
    """Подставной коннектор «другая вики»: свой манифест, свой скрипт операций, страницы — в файле проекта, без сети."""
    man = {"id": "otherwiki", "title": "Other wiki (test)", "kind": "wiki", "role": "artifacts",
           "mirror": {"default_path": "Sources/OtherWiki", "state": "sync_state.md"},
           "run": {"script": "otherwiki_export.py", "command": "sync:otherwiki"},
           "operations": {"role": "wiki", "script": "otherwiki_ops.py",
                          "ops": ["site", "find_page", "get_page", "children", "create_page", "update_page"]}}
    (root / ".aurora/connectors/otherwiki.json").write_text(json.dumps(man), encoding="utf-8")
    (root / ".aurora/scripts/otherwiki_ops.py").write_text(OTHER_WIKI, encoding="utf-8")
    store = root / ".aurora/state/otherwiki.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"pages": {"1": {"id": "1", "title": "Истории", "space": "DOC", "parent": None,
                                                 "version": 1, "body": "", "by": "Коллега"}}, "log": []},
                                ensure_ascii=False), encoding="utf-8")
    return store


@test
def test_publish_goes_through_wiki_operations_and_names_no_product(_t):
    """В `publish_doc.py` нет ни REST-клиента Confluence, ни его адресов: вики — операции роли через `connector_ops`."""
    src = (SCRIPTS / "publish_doc.py").read_text(encoding="utf-8")
    for gone in ("confluence_export", "/rest/api/", "safe_urlopen", "urllib.request", "read_secret"):
        assert gone not in src, f"публикация снова ходит в вики мимо операций роли: {gone}"
    assert "import connector_ops as CO" in src and 'ROLE = "wiki"' in src and "class Wiki" in src
    for op in ("site", "find_page", "get_page", "children", "create_page", "update_page"):
        assert f'"{op}"' in src, op


@test
def test_another_wiki_connector_publishes_without_core_edits(tmp: Path):
    """Подставная «другая вики» с тем же контрактом: выбрана в настройках проекта (блок `connectors:`), и `ship:publish`
    создаёт страницу под корнем вида, ставит штамп с её адресом, повтор — `unchanged` без записи, правка — обновление
    поверх версии, чужая правка — `conflict` до `--force`. Ядро о коннекторе не знает ни строки; Confluence не нужен."""
    root = make_project(tmp)
    store = _other_wiki(root)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + KIND, encoding="utf-8")
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "US.md").write_text("шаблон", encoding="utf-8")
    core = list((KIT / "scripts").glob("*.py")) + [KIT / "cockpit" / "aurora_cockpit.py"]
    assert not any("otherwiki" in p.read_text(encoding="utf-8") for p in core), "ядро знает тестовый коннектор"
    rel = "Artifacts/us/US-1.md"
    path = W.put(root, rel, W.doc("type: us\nstatus: ready\n"))
    # без выбора вики объявляют двое — публикация не идёт и называет причину
    cp, res = W.publish(root, rel, "--apply")
    assert res["outcome"] == "blocked" and res["code"] == "ambiguous" and "otherwiki" in res["why"], cp.stdout + cp.stderr
    CO = T._mod("connector_ops")
    assert CO.save(str(root), {"wiki": "otherwiki"}, CO.state(str(root))["digest"])["ok"]
    cp, res = W.publish(root, rel, "--apply")
    assert cp.returncode == 0 and res["outcome"] == "ok" and res["action"] == "create", cp.stdout + cp.stderr
    data = json.loads(store.read_text(encoding="utf-8"))
    page = data["pages"]["2"]
    assert page["title"] == "Итог" and page["parent"] == "1" and "Платёж проводится" in page["body"], page
    assert "Допущения" not in page["body"], "кухня производства уехала на страницу"
    T._mod("aurora_common")
    import aurora_common as AC
    fm = AC.frontmatter(path.read_text(encoding="utf-8"))
    assert fm["confluence_page_id"] == "2" and fm["published_version"] == "1", fm
    assert fm["published_url"] == "https://wiki.example.org/pages/viewpage.action?pageId=2" and fm["published_space"] == "DOC"
    cp, res = W.publish(root, rel, "--apply")
    assert res["outcome"] == "unchanged" and json.loads(store.read_text(encoding="utf-8"))["log"] == [["create", "2"]], res
    path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
    cp, res = W.publish(root, rel, "--apply")
    assert res["outcome"] == "ok" and res["action"] == "update" and res["version"] == "2", cp.stdout + cp.stderr
    data = json.loads(store.read_text(encoding="utf-8"))
    data["pages"]["2"].update(version=3, by="Коллега", body=data["pages"]["2"]["body"] + "<p>руками</p>")
    store.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8").replace("три дня", "два дня"), encoding="utf-8")
    cp, res = W.publish(root, rel, "--apply")
    assert res["outcome"] == "conflict" and res["conflict"]["now"] == "3" and res["conflict"]["by"] == "Коллега", res
    assert "руками" in json.loads(store.read_text(encoding="utf-8"))["pages"]["2"]["body"], "чужая правка затёрта"
    cp, res = W.publish(root, rel, "--apply", "--force")
    assert res["outcome"] == "ok" and res["version"] == "4", cp.stdout + cp.stderr


def _both(tmp: Path, wiki, tracker=None, head: str = "type: us\nstatus: ready\nrelease: \"1.0\"\n"):
    rid = W.root_page(wiki)
    kinds = ('artifacts:\n  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
             f'    publish_url: "{wiki.url}/pages/viewpage.action?pageId={rid}"\n')
    root = W.project(tmp, wiki.url, kinds)
    if tracker:
        cfg = root / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            '    project_key: "T"\n', f'    base_url: "{tracker.url}"\n    project_key: "PRJA"\n'), encoding="utf-8")
    return root, W.put(root, "Artifacts/us/US-1.md", W.doc(head))


@test
def test_publish_links_the_page_to_the_document_task_once(tmp: Path):
    """Документ с `task_key` после удачной публикации получает в задаче ссылку на свою страницу — у активного трекера
    (`link_page`); повторная публикация с изменением вторую ссылку не заводит. Без `task_key` трекер не спрашивают."""
    wiki, tracker = W.Wiki(), T.Tracker()
    try:
        with T.env(**T.jira_env(tmp)):
            root, path = _both(tmp, wiki, tracker, 'type: us\nstatus: ready\ntask_key: PRJA-2\n')
            cp, res = W.publish(root, "Artifacts/us/US-1.md", "--apply")
            assert res["outcome"] == "ok" and res["task_link"] == {"key": "PRJA-2", "linked": True, "why": ""}, \
                cp.stdout + cp.stderr
            assert "Задача PRJA-2: страница связана." in cp.stdout, cp.stdout
            assert [r["object"]["url"] for r in tracker.links["PRJA-2"]] == [res["url"]], tracker.links
            path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
            cp, res = W.publish(root, "Artifacts/us/US-1.md", "--apply")
            assert res["outcome"] == "ok" and "связь уже была" in cp.stdout and len(tracker.links["PRJA-2"]) == 1, cp.stdout
            W.put(root, "Artifacts/us/US-2.md", W.doc("type: us\nstatus: ready\n", "# Второй\n\nТекст.\n"))
            asked = len(tracker.log)
            cp, res = W.publish(root, "Artifacts/us/US-2.md", "--apply")
            assert res["outcome"] == "ok" and res["task_link"] == {} and len(tracker.log) == asked, "трекер спросили зря"
    finally:
        wiki.close()
        tracker.close()


@test
def test_publish_without_a_tracker_says_one_line_and_still_publishes(tmp: Path):
    """Трекер не настроен (адреса нет) или его нет вовсе — одна строка о том, что связь не поставлена; страница
    опубликована, штамп записан, код 0."""
    wiki = W.Wiki()
    try:
        with T.env(**T.jira_env(tmp)):
            root, path = _both(tmp, wiki, None, 'type: us\nstatus: ready\ntask_key: PRJA-2\n')
            cp, res = W.publish(root, "Artifacts/us/US-1.md", "--apply")
            assert cp.returncode == 0 and res["outcome"] == "ok" and res["stamped"], cp.stdout + cp.stderr
            assert res["task_link"]["linked"] is False and res["task_link"]["why"] == "config", res["task_link"]
            lines = [ln for ln in cp.stdout.splitlines() if "Задача PRJA-2" in ln]
            assert len(lines) == 1 and "не связана" in lines[0], cp.stdout
            # трекера нет вовсе: коннектора с ролью `tasks` не осталось
            (root / ".aurora/connectors/jira-dc.json").unlink()
            path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
            cp, res = W.publish(root, "Artifacts/us/US-1.md", "--apply")
            assert res["outcome"] == "ok" and res["task_link"]["why"] == "no-tracker", res
            lines = [ln for ln in cp.stdout.splitlines() if "Задача PRJA-2" in ln]
            assert len(lines) == 1 and "трекер задач не подключён" in lines[0], cp.stdout
    finally:
        wiki.close()


@test
def test_publish_names_the_wiki_from_its_connector_before_any_request(tmp: Path):
    """Нет адреса вики или ключа — причина называет настройку и переменную из манифеста коннектора (`confluence.base_url`,
    `CONFLUENCE_PAT`), до первого запроса; сырой отказ коннектора без кода ответа — `network`, с кодом — `http`."""
    root = make_project(tmp)
    W.put(root, "Artifacts/reports/итог.md", W.doc("type: report\nstatus: ready\n"))
    env = {**os.environ, "CONFLUENCE_PAT": "", "CONFLUENCE_PERSONAL_TOKEN": "", "CONFLUENCE_USER": "", "CONFLUENCE_PASSWORD": ""}
    import subprocess
    import sys
    cp = subprocess.run([sys.executable, str(SCRIPTS / "publish_doc.py"), "Artifacts/reports/итог.md"], cwd=str(root),
                        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 1 and "нет confluence.base_url" in cp.stderr, cp.stderr
    P = T._mod("publish_doc")
    assert P.http_problem(P.WikiError({"kind": "net", "detail": "Connection reset"}), "запись", "Вики")[0] == "network"
    code, why = P.http_problem(P.WikiError({"kind": "net", "status": 503, "detail": "HTTP 503"}), "запись", "Вики")
    assert code == "http" and "503" in why and "Вики отказал (запись)" in why, why
    assert P.http_problem(P.WikiError({"kind": "unsafe", "detail": "чужой хост"}))[0] == "unsafe"
    code, why = P.http_problem(P.WikiError({"kind": "ambiguous", "error": "роль «wiki» объявляют несколько"}))
    assert code == "ambiguous" and "несколько" in why
