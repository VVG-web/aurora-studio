"""Проверки движка Aurora, часть 46: дерево страниц Confluence в синке.

Каркас и помощники — tests/harness.py. Страница видна в вебе, а её поддерева в зеркале нет:
сервер отдал список детей короче объявленного, пустым, оборванным или отказал на ветке (403, 404).
Здесь проверяется, что синк не рвётся на отказе, называет ветку, считает страницы и глубину по корню,
закрывает `--prune` и умеет печатать дерево без записи файлов (`--diagnose-tree`). Сервер подставной,
сеть наружу не ходит.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import os
import subprocess
import sys

from harness import SCRIPTS, test  # noqa: F401


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


# ------------------------------------------------------------------ Confluence: ветка дерева, которую сервер не отдал

def _wiki_api(CE, tree: dict, refuse: dict | None = None, cut: dict | None = None, per_page: int = 50):
    """Подставной сервер Confluence на настоящем клиенте: страницы и списки детей по `tree`.

    `tree` — {номер: (название, [номера детей])}. `refuse` — {номер: код HTTP}: список детей этой
    страницы отдаётся отказом. `cut` — {номер: (отдать, объявить)}: сервер отдаёт меньше записей, чем
    объявил в `size`. Пагинация — настоящая, по `_links.next`."""
    import urllib.error
    import urllib.parse
    refuse, cut = refuse or {}, cut or {}

    class FakeApi(CE.Api):
        paths: list = []

        def get(self, path):
            self.paths.append(path)
            u = urllib.parse.urlparse(path)
            q = urllib.parse.parse_qs(u.query)
            if u.path.endswith("/child/page"):
                pid = u.path.split("/")[4]
                if pid in refuse:
                    raise urllib.error.HTTPError(path, refuse[pid], "refused", {}, None)
                kids = [{"id": c, "title": tree[c][0], "version": {"number": 1}} for c in tree[pid][1]]
                if pid in cut:
                    given, declared = cut[pid]
                    return {"results": kids[:given], "start": 0, "limit": 50, "size": declared, "_links": {}}
                start, limit = int((q.get("start") or ["0"])[0]), min(int(q["limit"][0]), per_page)
                chunk = kids[start:start + limit]
                more = {"next": f"/rest/api/content/{pid}/child/page?limit={limit}&start={start + limit}"} \
                    if start + limit < len(kids) else {}
                return {"results": chunk, "start": start, "limit": limit, "size": len(chunk), "_links": more}
            if u.path.endswith("/child/attachment"):
                return {"results": [], "_links": {}}
            if u.path.startswith("/rest/api/user"):
                return {}
            pid = u.path.rsplit("/", 1)[-1]
            return {"title": tree[pid][0], "id": pid, "version": {"number": 1, "when": "2026-10-01T00:00:00.000Z"},
                    "body": {"storage": {"value": f"<p>Текст страницы {pid}.</p>"}}, "space": {"key": "S"},
                    "_links": {"webui": f"/p/{pid}"}, "ancestors": []}

    api = FakeApi("https://wiki.example.com", "")
    api.paths = []
    return api


# Дерево из абстрактных страниц: верхний уровень → разделы → подразделы → лист (четыре уровня).
WIKI_TREE = {"1": ("Страница верхнего уровня", ["2", "5"]), "2": ("Раздел 1", ["3", "4"]),
             "3": ("Подраздел 1", ["6"]), "4": ("Подраздел 2", []), "5": ("Раздел 2", []),
             "6": ("Лист", [])}


def _ce():
    CE = _mod("confluence_export")
    importlib.reload(CE)
    return CE


@test
def test_confluence_children_follow_next_links_and_report_the_server_sizes(tmp: Path):
    """Сто двадцать детей идут тремя ответами по `_links.next`, `size` сходится — список полон; повтор той же
    ссылки не крутит запрос вечно, а называется обещанным, но не полученным продолжением."""
    CE = _ce()
    tree = {"1": ("Страница верхнего уровня", [str(100 + i) for i in range(120)])}
    tree.update({str(100 + i): (f"Раздел {i}", []) for i in range(120)})
    api = _wiki_api(CE, tree)
    kids, rep = api.children_report("1")
    assert [k["id"] for k in kids] == [str(100 + i) for i in range(120)], "порядок и состав детей потеряны"
    assert rep["got"] == 120 and rep["size"] == 120 and rep["pages"] == 3 and rep["sized"] and not rep["more"], rep
    assert CE.children_verdict(rep) == ""
    assert [p.split("start=")[-1] if "start=" in p else "0" for p in api.paths] == ["0", "50", "100"], api.paths
    assert api.children("1") == kids, "прежний `children()` отдаёт тот же список"
    # сервер повторяет ту же ссылку: цикла нет, продолжение названо
    class Loop(CE.Api):
        def get(self, path):
            return {"results": [{"id": "9", "title": "Раздел"}], "size": 1, "limit": 50,
                    "_links": {"next": "/rest/api/content/1/child/page?limit=50&start=1"}}
    kids, rep = Loop("https://wiki.example.com", "").children_report("1")
    assert rep["more"] and len(kids) == 2 and "_links.next" in CE.children_verdict(rep), (kids, rep)
    # сервер объявил общее число (больше страницы) в каждом ответе — это не нехватка детей
    class Total(CE.Api):
        def get(self, path):
            start = int(path.split("start=")[-1]) if "start=" in path else 0
            nxt = {"next": f"/x?start={start + 2}"} if start == 0 else {}
            return {"results": [{"id": str(start + i), "title": "Р"} for i in range(2)], "size": 120,
                    "limit": 50, "_links": nxt}
    kids, rep = Total("https://wiki.example.com", "").children_report("1")
    assert len(kids) == 4 and rep["total"] == 120 and rep["size"] == 0
    assert CE.children_verdict(rep) == "сервер отдал 4 из 120", "общее число должно сравниваться с полученным"


@test
def test_confluence_walk_names_the_branch_the_server_refused_or_cut(tmp: Path):
    """Отказ 403 на списке детей не рвёт синк: страница записана, ветка названа («ветка …: отказ 403»), остальные
    ветки выгружены, ошибка посчитана и чистка закрыта. Пустой список при объявленных трёх — «сервер отдал 0 из 3»."""
    import contextlib
    import io
    CE = _ce()
    root = tmp / "mirror-project"
    root.mkdir()
    here = os.getcwd()
    os.chdir(root)
    try:
        # 1. отказ 403 на детях подраздела
        api = _wiki_api(CE, WIKI_TREE, refuse={"3": 403})
        exp = CE.Exporter(api, str(root / "Sources/Confluence"), "https://wiki.example.com", "S", True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exp.walk("1", [])
        text = out.getvalue()
        assert "ветка Подраздел 1: отказ 403 (нет доступа под этим токеном)" in text, text
        assert {r[0] for r in exp.records} == {"1", "2", "3", "4", "5"}, "страница с отказом и её соседи должны быть выгружены"
        assert exp.branch_refused == 1 and exp.failed == 1 and exp.stat["pages"] == 5, (exp.branch_refused, exp.failed, exp.stat)
        assert exp.stat["depth"] == 3 and exp.stat["parents"] == 2, exp.stat
        ok, why = CE.prune_allowed(["Sources/Confluence/лишнее.md"], 5, exp.failed, exp.branch_refused)
        assert ok is False and "ошибками" in why, why
        # 2. пустой список при size > 0: ошибки запроса нет, но ветка неполна
        api = _wiki_api(CE, WIKI_TREE, cut={"1": (0, 3)})
        exp = CE.Exporter(api, str(root / "Sources/Other"), "https://wiki.example.com", "S", True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exp.walk("1", [])
        assert "ветка Страница верхнего уровня: сервер отдал 0 из 3" in out.getvalue(), out.getvalue()
        assert exp.branch_refused == 1 and exp.failed == 0 and {r[0] for r in exp.records} == {"1"}
        ok, why = CE.prune_allowed(["a.md"], 100, exp.failed, exp.branch_refused)
        assert ok is False and "ветки с отказом сервера (1)" in why and "--diagnose-tree" in why, why
        assert CE.prune_allowed(["a.md"], 100, 0, 0)[0] is True, "без отказов чистка разрешена по-прежнему"
    finally:
        os.chdir(here)


@test
def test_confluence_unchanged_page_still_reports_a_short_child_list(tmp: Path):
    """Неизменённая страница берётся из кэша, но список детей у сервера перечитывается — и сокращённый список
    называется (так терялось поддерево: «Раздел 1» не менялся, его дети — тоже, а сервер отдал одного из двух)."""
    import contextlib
    import io
    CE = _ce()
    root = tmp / "cached-project"
    root.mkdir()
    here = os.getcwd()
    os.chdir(root)
    try:
        out_dir = str(root / "Sources/Confluence")
        first = CE.Exporter(_wiki_api(CE, WIKI_TREE), out_dir, "https://wiki.example.com", "S", False)
        with contextlib.redirect_stdout(io.StringIO()):
            first.walk("1", [])
        first.save_cache()
        assert first.branch_refused == 0 and first.stat["pages"] == 6 and first.stat["depth"] == 4, first.stat
        api = _wiki_api(CE, WIKI_TREE, cut={"2": (1, 2)})
        second = CE.Exporter(api, out_dir, "https://wiki.example.com", "S", False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            second.walk("1", [])
        assert second.skipped >= 1, "страницы должны были взяться из кэша, иначе проверка ничего не доказывает"
        assert "ветка Раздел 1: сервер отдал 1 из 2" in out.getvalue(), out.getvalue()
        assert second.branch_refused == 1
        assert any(p.startswith("/rest/api/content/2/child/page") for p in api.paths), \
            "список детей неизменённой страницы не перечитан"
    finally:
        os.chdir(here)


@test
def test_confluence_sync_prints_per_root_counts_and_the_refused_branch_total(tmp: Path):
    """Сводка синка: по каждому корню — страниц, максимальная глубина, страниц с детьми; в итоге —
    «ветки с отказом сервера: N» и их перечень; строки идут в stdout, то есть в «Консоль» панели."""
    import contextlib
    import io
    CE = _ce()
    root = tmp / "summary-project"
    (root / ".aurora").mkdir(parents=True)
    (root / "aurora.config.yaml").write_text(
        'atlassian:\n  confluence:\n    base_url: "https://wiki.example.com"\n    space: "S"\n'
        '    sync_roots:\n      - page_id: "1"\n', encoding="utf-8")
    (root / ".env.aurora.local").write_text("CONFLUENCE_PAT=test-token\n", encoding="utf-8")
    here, argv, real_api = os.getcwd(), sys.argv, CE.Api
    api = _wiki_api(CE, WIKI_TREE, refuse={"3": 403})
    os.chdir(root)
    try:
        CE.Api = lambda base, auth, root="": api
        sys.argv = ["confluence_export.py"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = CE.main()
        text = out.getvalue()
        assert rc == 1, "отказ сервера на ветке — ошибка выгрузки (код возврата 1)"
        assert "Корень 1 (Страница верхнего уровня): страниц 5 · максимальная глубина 3 · страниц с детьми 2" in text, text
        assert "· ошибок: 1 · ветки с отказом сервера: 1" in text, text
        assert "Ветки, где сервер не отдал детей целиком (1):\n  - ветка Подраздел 1: отказ 403" in text, text
        assert "--diagnose-tree" in text
    finally:
        CE.Api, sys.argv = real_api, argv
        os.chdir(here)
        importlib.reload(CE)


@test
def test_confluence_diagnose_tree_prints_children_per_page_and_writes_nothing(tmp: Path):
    """`--diagnose-tree`: дерево корня с числом детей, пометкой отказа и сверкой с `size`; файлов не пишет и
    конвертер страниц не требует. Настоящий запрос по HTTP на подставной сервер: пагинация `_links.next` на
    ста двадцати детях, отказ 403 на одной ветке, пустой список при `size` > 0 на другой."""
    import http.server
    import threading
    import urllib.parse
    tree = {"1": ("Страница верхнего уровня", [str(100 + i) for i in range(120)])}
    tree.update({str(100 + i): (f"Раздел {i}", []) for i in range(120)})
    tree["100"] = ("Раздел 0", ["300", "301"])
    tree["300"], tree["301"] = ("Подраздел 1", []), ("Подраздел 2", [])

    class Wiki(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            parts = u.path.split("/")
            if u.path.endswith("/child/page"):
                pid = parts[4]
                if pid == "101":
                    return self.reply(403, {"message": "denied"})
                if pid == "102":
                    return self.reply(200, {"results": [], "size": 3, "limit": 50, "_links": {}})
                kids = [{"id": c, "title": tree[c][0], "version": {"number": 1}} for c in tree[pid][1]]
                start = int((q.get("start") or ["0"])[0])
                chunk = kids[start:start + 50]
                more = {"next": f"/rest/api/content/{pid}/child/page?limit=50&start={start + 50}"} \
                    if start + 50 < len(kids) else {}
                return self.reply(200, {"results": chunk, "start": start, "limit": 50, "size": len(chunk),
                                        "_links": more})
            self.reply(200, {"title": tree[parts[-1]][0], "id": parts[-1]})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Wiki)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = tmp / "diagnose-project"
    root.mkdir()
    (root / "aurora.config.yaml").write_text(
        f'atlassian:\n  confluence:\n    base_url: "http://127.0.0.1:{srv.server_address[1]}"\n    space: "S"\n'
        '    sync_roots:\n      - page_id: "1"\n', encoding="utf-8")
    (root / ".env.aurora.local").write_text("CONFLUENCE_PAT=test-token\n", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "AURORA_TESTS_ISOLATED": "1"}
    try:
        cp = subprocess.run([sys.executable, str(SCRIPTS / "confluence_export.py"), "--diagnose-tree"],
                            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
                            env=env, timeout=120)
    finally:
        srv.shutdown()
    out = cp.stdout.replace("\r\n", "\n")
    assert cp.returncode == 0, out + cp.stderr
    assert "Страница верхнего уровня (1) — детей: 120" in out, out
    assert "  Раздел 0 (100) — детей: 2\n    Подраздел 1 (300) — детей: 0" in out, out
    assert "Раздел 1 (101) — детей: 0 ⚠️ отказ 403 (нет доступа под этим токеном)" in out, out
    assert "Раздел 2 (102) — детей: 0 ⚠️ сервер отдал 0 из 3" in out, out
    assert ("Корень 1 (Страница верхнего уровня): страниц 123 · максимальная глубина 3 · "
            "страниц с детьми 2 · ветки с отказом сервера 2") in out, out
    assert sorted(p.name for p in root.iterdir() if not p.name.startswith(".")) == ["aurora.config.yaml"], \
        "диагностика дерева записала файлы"
    assert not (root / "Sources").exists()
    help_text = subprocess.run([sys.executable, str(SCRIPTS / "confluence_export.py"), "--help"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "--diagnose-tree" in help_text
    # без терминала: кнопка в карточке зеркала, которая есть только у команды с этим флагом
    view = (SCRIPTS.parent / "cockpit/modules/mirrors/view.js").read_text(encoding="utf-8")
    assert '(cmdRow.flags || []).includes("--diagnose-tree")' in view and 'ctx.openRun(inst.command, ["--diagnose-tree"])' in view
    for lang in ("ru", "en"):
        cat = json.loads((SCRIPTS.parent / f"cockpit/modules/mirrors/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert cat.get("mirrors.tree") and cat.get("mirrors.tree_hint"), lang
