"""Проверки движка Aurora, часть 96: выгрузчики и путь, который не помещается в систему.

Каркас и помощники — tests/harness.py. Windows без длинных путей не создаёт файл с путём длиннее
259 знаков, macOS и Linux — с именем длиннее 255 байт. Раньше такая запись обрывала весь синк
трейсбеком, и вместе с одной страницей пропадали все остальные. Правило выгрузчика: синк идёт
дальше, потеря названа строкой в журнале и счётчиком в сводке, чистка устаревшего в этом прогоне
не выполняется (недозаписанное выглядело бы удалённым). Сети и настоящего домашнего каталога
проверки не касаются: имя в 300 знаков даёт ту же ошибку «имя слишком длинно», что и длинный путь.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import contextlib
import errno
import http.server
import importlib
import io
import os
import subprocess
import sys
import textwrap
import threading

from harness import KIT, SCRIPTS, make_project, test  # noqa: F401

LONG = "x" * 300           # имя длиннее 255: ENAMETOOLONG на macOS и Linux, «путь не найден» на Windows


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


@test
def test_a_path_that_does_not_fit_is_told_apart_from_other_write_errors(tmp: Path):
    """Длина пути узнаётся по ошибке системы; нет места или прав — не она, и такая ошибка не глотается."""
    core = _mod("sources_core")
    assert core.path_overflow(OSError(errno.ENAMETOOLONG, "File name too long"), "/x")
    assert not core.path_overflow(OSError(errno.EACCES, "Permission denied"), "/x")
    assert not core.path_overflow(OSError(errno.ENOSPC, "No space left"), "/x")
    assert not core.path_overflow(ValueError("x"), "/x")
    win = FileNotFoundError(2, "Не удаётся найти указанный путь")
    win.winerror = 3
    assert core.path_overflow(win, "C:\\" + "p" * 250), "WinError 3 при длинном пути — это длина"
    assert not core.path_overflow(win, "C:\\short"), "WinError 3 при коротком пути — просто нет папки"
    win206 = OSError(0, "Имя слишком длинно")
    win206.winerror = 206
    assert core.path_overflow(win206, "")
    lost: list = []
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        for i in range(8):
            core.lose_path(lost, f"страница {i}", "/p/" + "a" * 300)
    assert len(lost) == 8 and err.getvalue().count("не помещается") == 5, "строк в журнале не больше пяти"
    line = core.loss_summary(lost, "файлов зеркала")
    assert line.startswith("Длинные пути: не записано 8 файлов зеркала") and "kit:paths" in line, line
    assert core.loss_summary([]) == ""


@test
def test_confluence_sync_goes_on_when_one_page_path_does_not_fit(tmp: Path):
    """Страница с непомещающимся путём не записана и названа; соседние и дочерние пишутся, трейсбека нет."""
    CE = _mod("confluence_export")
    root = make_project(tmp)
    tree = {"1": ("Корень", ["2", "3"]), "2": ("Длинная", ["4"]), "3": ("Соседняя", []), "4": ("Дочерняя", [])}

    class Api:
        def page(self, pid):
            return {"title": tree[pid][0], "version": {"number": 1, "when": "2026-10-19"},
                    "body": {"storage": {"value": "<p>Текст страницы.</p>"}}, "space": {"key": "S"},
                    "_links": {"webui": f"/p/{pid}"}}

        def children(self, pid):
            return [{"id": c, "title": tree[c][0], "version": {"number": 1}} for c in tree[pid][1]]

        def attachments(self, pid):
            return {}

        def user_name(self, key):
            return key

    here = os.getcwd()
    os.chdir(root)
    try:
        exp = CE.Exporter(Api(), str(root / "Sources/Confluence"), "https://wiki.example.com", "S", True)
        real_place = exp.place

        def place(title, page_id, parent, has_children, has_assets=False):
            return LONG if page_id == "2" else real_place(title, page_id, parent, has_children, has_assets)
        exp.place = place
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            exp.walk("1", [])
    finally:
        os.chdir(here)
    names = {r[0] for r in exp.records}
    assert names == {"1", "3", "4"}, f"страница 2 не записана, остальные — да: {names}"
    assert len(exp.path_lost) == 1 and exp.failed == 0, (exp.path_lost, exp.failed)
    assert "страница 2" in err.getvalue() and "не помещается" in err.getvalue(), err.getvalue()
    assert "2" in exp.nocache, "страница без файла не должна считаться свежей в кэше"
    assert (root / "Sources/Confluence/Соседняя.md").is_file() or any(
        p.name.startswith("Соседн") for p in (root / "Sources/Confluence").rglob("*.md"))


@test
def test_confluence_main_names_the_loss_and_holds_back_the_cleanup(tmp: Path):
    """Итог синка называет число непоместившихся файлов и код 0; `--prune` устаревшее при потерях не трогает."""
    root = make_project(tmp)
    stale = root / "Sources/Confluence/Старая.md"
    stale.write_text("---\npage_id: 9\n---\n\nстарая\n", encoding="utf-8")
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(SCRIPTS)!r})
        import confluence_export as C
        C.read_config = lambda: {{"base_url": "https://wiki", "space": "S", "out": "Sources/Confluence",
                                 "roots": ["1"]}}
        C.read_secret = lambda: ("x", "токен")
        def fake(cfg, roots, out, auth, force):
            exp = C.Exporter(None, out, "https://wiki", "S", False)
            exp.records = [("1", "Корень.md", "Корень", "NEW")]
            exp.path_lost = ["/p/" + "a" * 300]
            return exp
        C.run_export = fake
        sys.argv = ["confluence_export.py", "--prune"]
        sys.exit(C.main())
    """)
    cp = subprocess.run([sys.executable, "-c", script], cwd=str(root), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "Длинные пути: не записано 1 файлов зеркала" in cp.stdout, cp.stdout
    assert "Не убираю:" in cp.stdout and stale.is_file(), "чистка при потерях не выполняется\n" + cp.stdout


@test
def test_web_sync_goes_on_when_a_page_name_does_not_fit(tmp: Path):
    """Сайт: страница с непомещающимся именем названа в итоге, её соседка сохранена, код возврата 0."""
    W = _mod("web_export")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            title = "Длинная" if self.path.startswith("/long") else "Короткая"
            data = f"<html><title>{title}</title><body><main><p>Текст.</p></main></body></html>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    cwd = os.getcwd()
    os.chdir(tmp)
    saved = {k: getattr(W, k) for k in ("config_text", "PAUSE", "ALLOW_PRIVATE", "slug")}
    W.config_text = lambda: (f"web:\n  allow_private: [127.0.0.1]\n  pages:\n    - url: {base}/long\n"
                             f"      trusted: true\n    - url: {base}/short\n      trusted: true\n")
    W.PAUSE = 0
    real_slug = W.slug
    W.slug = lambda url, title: LONG + ".md" if "/long" in url else real_slug(url, title)
    out = tmp / "Sources" / "Web"
    buf = io.StringIO()
    try:
        args = argparse.Namespace(out=str(out), apply=True, prune=False, verify=False, force=True)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = W.run(args)
    finally:
        srv.shutdown()
        os.chdir(cwd)
        for k, v in saved.items():
            setattr(W, k, v)
    text = buf.getvalue()
    assert rc == 0, text
    assert len(W.PATH_LOST) == 1 and "Длинные пути: не записано 1 файлов зеркала" in text, text
    assert "путь не помещается в эту систему, страница не сохранена" in text, text
    saved_pages = [p for p in out.glob("*.md") if p.name not in ("update_log.md", "sync_state.md")]
    assert len(saved_pages) == 1, [p.name for p in saved_pages]
    state = (out / "update_log.md").read_text(encoding="utf-8")
    assert "длинный путь" in state and "страниц: 1 " in text, (state, text)


@test
def test_jira_sync_goes_on_when_an_issue_path_does_not_fit(tmp: Path):
    """Jira: задача, которую нельзя записать по длине пути, названа в итоге, остальные записаны."""
    je = _mod("jira_export")
    issues = [{"key": f"PRJ-{n}", "fields": {"summary": f"Задача {n}", "updated": "2026-10-19T10:00:00.000+0000",
                                             "status": {"name": "Готово"}, "description": "текст"}}
              for n in (1, 2, 3)]

    class Api:
        def __init__(self, *a, **k):
            pass

        def epic_field(self):
            return ""

        def search(self, jql, fields, limit=0):
            return issues

    saved = (je.Api, getattr(je, "open", None))
    real_open = open

    def guarded(path, mode="r", *a, **k):
        if "w" in mode and str(path).endswith("PRJ-2.md"):
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(path))
        return real_open(path, mode, *a, **k)
    je.Api = Api
    je.open = guarded
    out = tmp / "Sources" / "JIRA"
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            rows, written, skipped = je.run_export({"base_url": "https://jira.example.com"}, "x", str(out),
                                                   "project = PRJ", 0, True, False)
    finally:
        je.Api = saved[0]
        if saved[1] is None:
            del je.open
        else:
            je.open = saved[1]
    assert written == 2 and sorted(p.name for p in out.glob("PRJ-*.md")) == ["PRJ-1.md", "PRJ-3.md"], \
        (written, [p.name for p in out.iterdir()])
    assert len(je.PATH_LOST) == 1 and "задача PRJ-2" in err.getvalue(), err.getvalue()
    assert je.loss_summary(je.PATH_LOST, "задач").startswith("Длинные пути: не записано 1 задач")
    state = je.write_state(str(out), rows)
    assert sorted(state) == ["PRJ-1", "PRJ-3"], "в состоянии нет задачи, которой нет на диске"


@test
def test_a_zone_that_cannot_be_created_ends_the_sync_with_one_line(tmp: Path):
    """Папка зеркала не создаётся вовсе: одна строка причины и код 1, а не трейсбек."""
    core = _mod("sources_core")
    err = io.StringIO()

    def main():
        raise OSError(errno.ENAMETOOLONG, "File name too long", "/p/" + "a" * 300)
    with contextlib.redirect_stderr(err):
        rc = core.run_main(main)
    assert rc == 1 and "путь не помещается в эту систему" in err.getvalue() and "kit:paths" in err.getvalue()

    def other():
        raise OSError(errno.EACCES, "Permission denied", "/p")
    try:
        core.run_main(other)
    except OSError as e:
        assert e.errno == errno.EACCES
    else:
        raise AssertionError("другая ошибка записи не должна глотаться")
