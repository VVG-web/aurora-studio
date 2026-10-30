"""Проверки движка Aurora, часть 170. Каркас и помощники — tests/harness.py.

Заменяемые коннекторы (фаза 1): ядро знает роли (`tasks` — трекер задач, `wiki` — вики) и имена операций, а не
коннекторы. Активный коннектор роли — блок `connectors:` конфига; нет блока — единственный, кто объявляет роль.
Запись блока — со слепком (`expect`), чужой конфиг остаётся байт в байт. Коннектор без операции — понятный отказ.
Главное требование владельца — подставной «другой трекер» с тем же контрактом работает без правки ядра.

Здесь же — подставная Jira (`Tracker`) для частей 171, 172 и 174: проекты и типы задач, поиск эпиков постранично,
поля (Epic Link по схеме, по имени или без него), поиск людей, создание задач, remote links. Сеть — только 127.0.0.1,
проекты — во временных папках, настоящий HOME не трогается.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test

TOKEN = "test-token-not-real"
MARK = "AURORA-TASK "


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


@contextlib.contextmanager
def env(**kw):
    """Переменные окружения на время блока (вызовы операций — дочерние процессы, они их наследуют)."""
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: str(v) for k, v in kw.items()})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def jira_env(tmp: Path) -> dict:
    """Ключ подставной Jira и пустой git человека (HOME — временный): почта исполнителя только из проекта."""
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    return {"JIRA_PAT": TOKEN, "JIRA_PERSONAL_TOKEN": "", "JIRA_USER": "", "JIRA_PASSWORD": "",
            "HOME": str(home), "USERPROFILE": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": str(home / ".config")}


# ------------------------------------------------------------------ подставная Jira

class Tracker:
    """Подставная Jira Data Center на 127.0.0.1. `log` — (метод, путь, тело, Authorization).

    `epic_mode`: `schema` — поле Epic Link узнаётся по схеме (рядом приманка «Epic Link» с чужой схемой), `name` — только
    по имени, `none` — поля нет (эпик — родителем). `fail` — разовые ответы ошибкой `[(метод, кусок пути, код)]`;
    `page_size` — сколько задач в одном ответе поиска."""

    def __init__(self, epic_mode: str = "schema"):
        self.epic_mode = epic_mode
        self.types = [{"id": "10001", "name": "Story", "subtask": False, "description": "история"},
                      {"id": "10000", "name": "Epic", "subtask": False, "description": ""},
                      {"id": "10003", "name": "Sub-task", "subtask": True, "description": ""},
                      {"id": "10002", "name": "Task", "subtask": False, "description": ""}]
        self.issues: dict = {}
        self.links: dict = {}
        self.users = [{"name": "analyst", "key": "JIRAUSER10", "displayName": "Analyst A",
                       "emailAddress": "analyst@example.com"},
                      {"name": "lead", "key": "JIRAUSER11", "displayName": "Lead B", "emailAddress": "lead@example.com"}]
        self.log: list = []
        self.fail: list = []
        self.page_size = 2
        self.seq = 0
        self.lock = threading.Lock()
        for key, title, status, cat in (("PRJA-3", "Новый эпик", "To Do", "new"),
                                        ("PRJA-2", "Открытый эпик", "In Progress", "indeterminate"),
                                        ("PRJA-1", "Закрытый эпик", "Done", "done")):
            self._add(key, {"summary": title, "issuetype": {"id": "10000", "name": "Epic"},
                            "status": {"name": status, "statusCategory": {"key": cat}}})
        self._add("PRJA-4", {"summary": "Просто история", "issuetype": {"id": "10001", "name": "Story"},
                             "status": {"name": "To Do", "statusCategory": {"key": "new"}}})
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _failed(self, method: str) -> bool:
                for i, (m, part, code) in enumerate(outer.fail):
                    if m == method and part in self.path:
                        del outer.fail[i]
                        self._send(code, {"errorMessages": [f"подставная ошибка {code}"], "errors": {}})
                        return True
                return False

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                outer.log.append(("GET", self.path, None, self.headers.get("Authorization")))
                if self._failed("GET"):
                    return
                p = u.path
                with outer.lock:
                    m = re.fullmatch(r"/rest/api/2/project/([A-Z0-9_]+)", p)
                    if m:
                        if m.group(1) != "PRJA":
                            return self._send(404, {"errorMessages": ["No project could be found with key"]})
                        return self._send(200, {"key": "PRJA", "issueTypes": outer.types})
                    if p == "/rest/api/2/field":
                        return self._send(200, outer.fields())
                    if p == "/rest/api/2/search":
                        return self._send(200, outer.search(q))
                    if p == "/rest/api/2/user/search":
                        who = (q.get("username") or [""])[0].casefold()
                        return self._send(200, [u_ for u_ in outer.users if who and (
                            who in u_["emailAddress"].casefold() or who in u_["name"].casefold())])
                    m = re.fullmatch(r"/rest/api/2/issue/([A-Z0-9_]+-\d+)/remotelink", p)
                    if m:
                        if m.group(1) not in outer.issues:
                            return self._send(404, {"errorMessages": ["Issue Does Not Exist"]})
                        return self._send(200, outer.links.get(m.group(1), []))
                    m = re.fullmatch(r"/rest/api/2/issue/([A-Z0-9_]+-\d+)", p)
                    if m:
                        it = outer.issues.get(m.group(1))
                        return self._send(200, it) if it else self._send(404, {"errorMessages": ["Issue Does Not Exist"]})
                return self._send(404, {"errorMessages": [f"нет такого пути {p}"]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
                outer.log.append(("POST", self.path, payload, self.headers.get("Authorization")))
                if self._failed("POST"):
                    return
                p = urllib.parse.urlparse(self.path).path
                with outer.lock:
                    if p == "/rest/api/2/issue":
                        code, obj = outer.create(payload.get("fields") or {})
                        return self._send(code, obj)
                    m = re.fullmatch(r"/rest/api/2/issue/([A-Z0-9_]+-\d+)/remotelink", p)
                    if m:
                        rows = outer.links.setdefault(m.group(1), [])
                        same = next((r for r in rows if r.get("globalId") == payload.get("globalId")), None)
                        if same:                              # как настоящая Jira: тот же globalId — обновление
                            same["object"] = payload["object"]
                            return self._send(200, {"id": same["id"]})
                        outer.seq += 1
                        rows.append({"id": outer.seq, "globalId": payload.get("globalId"), "object": payload["object"]})
                        return self._send(201, {"id": outer.seq, "self": f"/rest/api/2/issue/{m.group(1)}/remotelink/{outer.seq}"})
                return self._send(404, {"errorMessages": [f"нет такого пути {p}"]})

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def _add(self, key: str, fields: dict) -> None:
        self.seq += 1
        self.issues[key] = {"id": str(self.seq), "key": key, "fields": dict(fields, updated=self.seq)}

    def fields(self) -> list:
        base = [{"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string", "system": "summary"}}]
        if self.epic_mode == "schema":
            return base + [{"id": "customfield_10999", "name": "Epic Link", "custom": True,
                            "schema": {"type": "string", "custom": "com.example.decoy:text"}},
                           {"id": "customfield_10500", "name": "Связь с эпиком", "custom": True,
                            "schema": {"type": "any", "custom": "com.pyxis.greenhopper.jira:gh-epic-link"}}]
        if self.epic_mode == "name":
            return base + [{"id": "customfield_10600", "name": "Epic Link", "custom": True, "schema": {"type": "any"}}]
        return base

    def search(self, q: dict) -> dict:
        jql = (q.get("jql") or [""])[0]
        proj = re.search(r'project = "?([A-Z0-9_]+)"?', jql)
        kind = re.search(r"issuetype = ([\w-]+)", jql)
        rows = [it for it in self.issues.values()
                if (not proj or it["key"].startswith(proj.group(1) + "-"))
                and (not kind or kind.group(1) in (it["fields"]["issuetype"]["id"], it["fields"]["issuetype"]["name"]))]
        rows.sort(key=lambda it: -it["fields"]["updated"])
        start = int((q.get("startAt") or ["0"])[0])
        size = min(int((q.get("maxResults") or ["50"])[0]), self.page_size)
        return {"startAt": start, "maxResults": size, "total": len(rows), "issues": rows[start:start + size]}

    def create(self, f: dict) -> tuple:
        key = (f.get("project") or {}).get("key")
        if key != "PRJA":
            return 400, {"errorMessages": [], "errors": {"project": "project is required"}}
        kind = f.get("issuetype") or {}
        hit = next((t for t in self.types if t["id"] == kind.get("id") or t["name"] == kind.get("name")), None)
        if not hit or not f.get("summary"):
            return 400, {"errorMessages": [], "errors": {"issuetype": "valid issue type is required"}}
        who = (f.get("assignee") or {}).get("name")
        if who and who not in {u["name"] for u in self.users}:
            return 400, {"errorMessages": [], "errors": {"assignee": f"User '{who}' does not exist."}}
        n = len([k for k in self.issues if k.startswith("PRJA-")]) + 1
        new = f"PRJA-{n}"
        self._add(new, dict(f, issuetype={"id": hit["id"], "name": hit["name"]},
                            status={"name": "To Do", "statusCategory": {"key": "new"}}))
        return 201, {"id": self.issues[new]["id"], "key": new, "self": f"/rest/api/2/issue/{new}"}

    def created(self) -> list:
        return [b for m, p, b, _a in self.log if m == "POST" and urllib.parse.urlparse(p).path == "/rest/api/2/issue"]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def jira_project(tmp: Path, url: str, extra: str = "", sub: str = "") -> Path:
    """Проект с адресом подставной Jira и ключом PRJA в `atlassian.jira`."""
    if sub:
        tmp = tmp / sub
        tmp.mkdir()
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    text = cfg.read_text(encoding="utf-8").replace('    project_key: "T"\n',
                                                   f'    base_url: "{url}"\n    project_key: "PRJA"\n')
    cfg.write_text(text + extra, encoding="utf-8")
    return root


def ship_task(root: Path, rel: str, *extra: str, environ: dict | None = None) -> tuple:
    """`ship:task` процессом → (процесс, исход из машинной строки `AURORA-TASK`)."""
    cp = subprocess.run([sys.executable, str(SCRIPTS / "task_doc.py"), rel, *extra], cwd=str(root),
                        env={**os.environ, **(environ or {})}, capture_output=True, text=True, encoding="utf-8",
                        errors="replace")
    line = next((ln for ln in cp.stdout.splitlines() if ln.startswith(MARK)), "")
    return cp, (json.loads(line[len(MARK):]) if line else {})


# ------------------------------------------------------------------ выбор активного коннектора

@test
def test_connector_role_defaults_to_the_single_connector_that_declares_it(tmp: Path):
    """Блока `connectors:` нет — роль играет единственный установленный коннектор, что её объявляет: трекер задач —
    коннектор с `operations.role: tasks`, вики — с `wiki`. Манифест без поля `operations` (копия старше поля) роль
    получает по роли сервиса, а операции не умеет."""
    CO = _mod("connector_ops")
    root = make_project(tmp)
    mods = {m["id"]: m for m in CO.candidates(str(root), "tasks") + CO.candidates(str(root), "wiki")}
    tasks, wiki = CO.active(str(root), "tasks"), CO.active(str(root), "wiki")
    assert tasks and wiki and tasks != wiki, (tasks, wiki)
    assert mods[tasks]["operations"]["role"] == "tasks" and mods[wiki]["operations"]["role"] == "wiki"
    m, fail, src = CO.pick(str(root), "tasks")
    assert m and fail is None and src == "single"
    assert CO.supports(str(root), "tasks", "create_issue") and CO.supports(str(root), "wiki", "update_page")
    assert not CO.supports(str(root), "tasks", "update_page"), "операция другой роли засчитана"
    assert CO.defaults(str(root), "tasks") == {"project_key": "T"}, CO.defaults(str(root), "tasks")
    # копия манифеста без `operations`: роль — по роли сервиса, операции — нет
    man = root / ".aurora" / "connectors" / f"{tasks}.json"
    old = json.loads(man.read_text(encoding="utf-8"))
    old.pop("operations")
    man.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    assert CO.active(str(root), "tasks") == tasks and not CO.supports(str(root), "tasks", "issue_types")
    res = CO.call(str(root), "tasks", "issue_types", {"project_key": "T"})
    assert not res["ok"] and res["kind"] == "unsupported" and "Подготовить проект" in res["error"], res
    bad = CO.call(str(root), "nonsense", "issue_types")
    assert not bad["ok"] and bad["kind"] == "bad_role" and "tasks" in bad["error"], bad


@test
def test_connector_role_comes_from_the_config_block_and_refuses_unknown_and_ambiguous(tmp: Path):
    """Блок `connectors:` выбирает коннектор роли. Выбран неустановленный — отказ с причиной, а не тихая подмена;
    роль объявляют двое, а выбора нет — отказ «несколько коннекторов», операция не идёт."""
    CO = _mod("connector_ops")
    root = make_project(tmp)
    tasks = CO.active(str(root), "tasks")
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    cfg.write_text(base + f"connectors:\n  tasks: {tasks}   # выбран человеком\n", encoding="utf-8")
    assert CO.pick(str(root), "tasks")[2] == "config" and CO.active(str(root), "tasks") == tasks
    cfg.write_text(base + "connectors:\n  tasks: no-such-tracker\n", encoding="utf-8")
    m, fail, _src = CO.pick(str(root), "tasks")
    assert m is None and fail["kind"] == "unknown_connector" and "no-such-tracker" in fail["error"], fail
    assert CO.active(str(root), "tasks") == ""
    # второй коннектор с ролью трекера — без выбора роль не играет никто
    cfg.write_text(base, encoding="utf-8")
    _other_tracker(root)
    m, fail, _src = CO.pick(str(root), "tasks")
    assert m is None and fail["kind"] == "ambiguous" and "othertrack" in fail["error"] and tasks in fail["error"], fail
    res = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJB"})
    assert not res["ok"] and res["kind"] == "ambiguous", res
    st = CO.state(str(root))
    assert st["roles"]["tasks"]["active"] == "" and st["roles"]["tasks"]["kind"] == "ambiguous", st
    assert {c["id"] for c in st["roles"]["tasks"]["choices"]} == {tasks, "othertrack"}, st


@test
def test_connector_roles_block_write_keeps_the_rest_of_the_config_and_refuses_a_stale_snapshot(tmp: Path):
    """Запись блока `connectors:` меняет только его: остальной конфиг с комментариями и CRLF — байт в байт. Слепок
    (`expect`) не тот или не прислан — конфликт, файл не тронут. Пустой id возвращает роли выбор по умолчанию, а
    пустой блок уходит из конфига целиком; неизвестный коннектор и неизвестная роль — отказ до записи."""
    CO = _mod("connector_ops")
    root = make_project(tmp)
    _other_tracker(root)
    cfg = root / "aurora.config.yaml"
    head = cfg.read_text(encoding="utf-8").replace("\n", "\r\n") + "# комментарий человека\r\nother:\r\n  key: 1\r\n"
    cfg.write_bytes(head.encode("utf-8"))
    st = CO.state(str(root))
    res = CO.save(str(root), {"tasks": "othertrack"}, st["digest"])
    assert res["ok"] and res["chosen"] == {"tasks": "othertrack"}, res
    after = cfg.read_bytes().decode("utf-8")
    assert after.startswith(head), "запись блока тронула остальной конфиг"
    assert "\r\nconnectors:" in after and "  tasks: othertrack\r\n" in after and "\n" not in after.replace("\r\n", ""), after
    assert CO.active(str(root), "tasks") == "othertrack"
    # устаревший слепок и запрос без слепка — конфликт, файл не тронут
    for expect in (st["digest"], None):
        stale = CO.save(str(root), {"tasks": ""}, expect)
        assert not stale["ok"] and stale["conflict"] and stale["code"] == "conflict", stale
        assert cfg.read_bytes().decode("utf-8") == after
    for roles, code in (({"tasks": "no-such"}, "unknown_connector"), ({"crm": "othertrack"}, "bad_role")):
        bad = CO.save(str(root), roles, res["digest"])
        assert not bad["ok"] and bad["code"] == code, bad
    # вернуть выбор по умолчанию — блок уходит, остальное как было
    back = CO.save(str(root), {"tasks": ""}, res["digest"])
    assert back["ok"] and back["chosen"] == {}, back
    assert cfg.read_bytes().decode("utf-8") == head, "пустой блок не ушёл из конфига или потянул чужое"


@test
def test_connector_without_the_operation_gives_a_clear_refusal(tmp: Path):
    """Коннектор не объявил операцию — ядро скрипт не зовёт и отвечает «не умеет» с названием коннектора и операции;
    объявил, а скрипт её не знает, — тот же вид отказа от самого скрипта; скрипта нет — «движок неполный»; скрипт
    ответил не по контракту — `bad_response`."""
    CO = _mod("connector_ops")
    root = make_project(tmp)
    ops = _other_tracker(root, ops=["issue_types", "epics"])
    _choose(root, "othertrack")
    res = CO.call(str(root), "tasks", "create_issue", {"project_key": "PRJB", "type": "Bug", "summary": "x"})
    assert not res["ok"] and res["kind"] == "unsupported" and "othertrack" in res["error"] \
        and "create_issue" in res["error"], res
    assert not (root / ".aurora/state/othertrack.json").exists(), "скрипт позвали за операцией, которой нет в манифесте"
    assert not CO.supports(str(root), "tasks", "create_issue") and CO.supports(str(root), "tasks", "epics")
    # объявлена, а в скрипте нет — отказ самого скрипта тем же видом
    man = root / ".aurora/connectors/othertrack.json"
    m = json.loads(man.read_text(encoding="utf-8"))
    m["operations"]["ops"].append("teleport")
    man.write_text(json.dumps(m), encoding="utf-8")
    res = CO.call(str(root), "tasks", "teleport", {})
    assert not res["ok"] and res["kind"] == "unsupported" and "teleport" in res["detail"], res
    # скрипт ответил не по контракту
    ops.write_text("print('привет')\n", encoding="utf-8")
    res = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJB"})
    assert not res["ok"] and res["kind"] == "bad_response", res
    ops.unlink()
    res = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJB"})
    assert not res["ok"] and res["kind"] == "no_script" and "Подготовить проект" in res["error"], res


# ------------------------------------------------------------------ подставной «другой трекер»

OTHER_OPS = '''import json
import os
import sys
from sources_core import OpFailure, serve_ops

STORE = os.path.join(".aurora", "state", "othertrack.json")


def _load(project):
    try:
        with open(os.path.join(project, STORE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"issues": [], "links": []}


def _save(project, data):
    path = os.path.join(project, STORE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def issue_types(params, project):
    return {"types": [{"id": "bug", "name": "Bug", "description": ""}, {"id": "job", "name": "Job", "description": ""}]}


def epics(params, project):
    return {"epics": [{"key": "PRJB-100", "title": "Big goal", "status": "open", "done": False}]}


def find_user(params, project):
    if params.get("email") == "lead@example.org":
        return {"name": "lead", "key": "lead", "display": "Lead", "email": "lead@example.org"}
    raise OpFailure("not_found", "no such person")


def create_issue(params, project):
    data = _load(project)
    key = "%s-%d" % (params["project_key"], len(data["issues"]) + 1)
    data["issues"].append(dict(params, key=key))
    _save(project, data)
    return {"key": key, "id": key, "url": "https://tracker.example.org/item/" + key, "epic_via": "epic"}


def link_page(params, project):
    data = _load(project)
    for link in data["links"]:
        if link["issue_key"] == params["issue_key"] and link["url"] == params["url"]:
            return {"id": link["id"], "created": False}
    data["links"].append(dict(params, id=str(len(data["links"]) + 1)))
    _save(project, data)
    return {"id": data["links"][-1]["id"], "created": True}


def get_issue(params, project):
    hit = next((i for i in _load(project)["issues"] if i["key"] == params.get("key")), None)
    if not hit:
        raise OpFailure("not_found", "no such issue")
    return {"key": hit["key"], "summary": hit["summary"], "status": "open", "status_category": "new",
            "type": hit["type"], "url": "https://tracker.example.org/item/" + hit["key"]}


OPS = {"issue_types": issue_types, "epics": epics, "find_user": find_user, "create_issue": create_issue,
       "link_page": link_page, "get_issue": get_issue}
if __name__ == "__main__":
    sys.exit(serve_ops(OPS))
'''


def _other_tracker(root: Path, ops: list | None = None) -> Path:
    """Тестовый коннектор «другой трекер»: свой манифест, свой блок настроек, свой скрипт операций (задачи — в файле
    проекта, без сети). Ядро о нём не знает ничего, кроме того, что он сам объявил."""
    man = {"id": "othertrack", "title": "Other tracker (test)", "kind": "board", "role": "tasks",
           "mirror": {"default_path": "Sources/Other", "state": "update_log.md"},
           "run": {"script": "othertrack_export.py", "command": "sync:othertrack"},
           "settings_block": "othertrack", "settings": [{"key": "project", "what": "project key"}],
           "operations": {"role": "tasks", "script": "othertrack_ops.py",
                          "ops": ops or ["issue_types", "epics", "find_user", "create_issue", "link_page", "get_issue"],
                          "server": "url", "defaults": {"project_key": "project"}}}
    (root / ".aurora/connectors/othertrack.json").write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    script = root / ".aurora/scripts/othertrack_ops.py"
    script.write_text(OTHER_OPS, encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    if "othertrack:" not in cfg.read_text(encoding="utf-8"):
        with open(cfg, "a", encoding="utf-8", newline="") as f:
            f.write('othertrack:\n  url: "https://tracker.example.org"\n  project: "PRJB"\n')
    return script


def _choose(root: Path, cid: str, role: str = "tasks") -> None:
    CO = _mod("connector_ops")
    st = CO.state(str(root))
    res = CO.save(str(root), {role: cid}, st["digest"])
    assert res["ok"], res


@test
def test_another_tracker_connector_works_through_the_same_contract_without_core_edits(tmp: Path):
    """Главное требование владельца: завтра вместо Jira — другой трекер. Подставной коннектор во временном проекте
    объявляет роль `tasks` и те же операции, ядро о нём не знает ни строки. Выбор в настройках проекта (блок
    `connectors:`) — и списки, поиск человека, `ship:task` с заведением задачи, штампом и связью страницы идут через
    него; Jira не нужна вовсе (её адреса в конфиге нет)."""
    CO = _mod("connector_ops")
    root = make_project(tmp)
    _other_tracker(root)
    core = [p for p in list((KIT / "scripts").glob("*.py")) + [KIT / "cockpit" / "aurora_cockpit.py"]]
    assert not any("othertrack" in p.read_text(encoding="utf-8") for p in core), "ядро знает тестовый коннектор"
    doc = root / "Artifacts" / "us" / "US-9.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: us\npublished_url: https://wiki.example.org/pages/9\ntask:\n  type: Job\n"
                   "  assignee: lead@example.org\n  labels: [alpha, beta]\n---\n\n# Платёж за пять дней\n\nТекст.\n",
                   encoding="utf-8")
    rel = "Artifacts/us/US-9.md"
    # без выбора — двое с ролью: команда останавливается и называет причину
    cp, res = ship_task(root, rel, "--apply")
    assert cp.returncode == 1 and res["outcome"] == "blocked" and res["code"] == "ambiguous", (cp.stdout, cp.stderr)
    _choose(root, "othertrack")
    assert CO.active(str(root), "tasks") == "othertrack"
    assert CO.defaults(str(root), "tasks") == {"project_key": "PRJB"}, "ключ проекта не из блока настроек коннектора"
    types = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJB"})
    assert types["ok"] and [t["name"] for t in types["result"]["types"]] == ["Bug", "Job"] and not types["cached"], types
    again = CO.call(str(root), "tasks", "issue_types", {"project_key": "PRJB"})
    assert again["ok"] and again["cached"] and again["result"] == types["result"], again
    cp, res = ship_task(root, rel)
    assert cp.returncode == 0 and res["outcome"] == "plan" and res["connector"] == "othertrack", (cp.stdout, cp.stderr)
    assert res["task"]["project"] == "PRJB" and res["task"]["assignee"] == "lead" and res["task"]["labels"] == ["alpha", "beta"], res
    assert not (root / ".aurora/state/othertrack.json").exists(), "план завёл задачу"
    cp, res = ship_task(root, rel, "--apply")
    assert cp.returncode == 0 and res["outcome"] == "ok" and res["key"] == "PRJB-1" and res["stamped"] and res["linked"], \
        (cp.stdout, cp.stderr)
    store = json.loads((root / ".aurora/state/othertrack.json").read_text(encoding="utf-8"))
    assert [i["key"] for i in store["issues"]] == ["PRJB-1"] and store["issues"][0]["type"] == "Job"
    assert store["issues"][0]["summary"] == "Платёж за пять дней" and store["issues"][0]["assignee"] == "lead", store
    assert store["links"][0]["url"] == "https://wiki.example.org/pages/9", store
    text = doc.read_text(encoding="utf-8")
    assert "task_key: PRJB-1\n" in text and "task_url: https://tracker.example.org/item/PRJB-1\n" in text, text
    cp, res = ship_task(root, rel, "--apply")
    assert res["outcome"] == "exists" and res["key"] == "PRJB-1", res
    store = json.loads((root / ".aurora/state/othertrack.json").read_text(encoding="utf-8"))
    assert len(store["issues"]) == 1 and len(store["links"]) == 1, "повтор завёл вторую задачу или ссылку"
