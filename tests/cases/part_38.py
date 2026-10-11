"""Проверки движка Aurora, часть 38: чужой текст в панели не становится кодом в её origin;
токен панели не отдаётся любому клиенту петли и не ездит в адресе.

Каркас и помощники — tests/harness.py. Зеркала Confluence и сайтов пишут посторонние, имена
людей в отчётах — пользователи чужой Jira. У origin панели токен сессии, а токен даёт запуск
команд, поэтому mermaid в редакторе строгий, у страницы есть CSP, а отчёт отдаётся в песочнице.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

from harness import KIT, SCRIPTS, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _server(ck, tmp):
    import threading
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path: str):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        c.request("GET", path, headers={"X-Aurora-Token": ck.TOKEN} if path.startswith("/api/") else {})
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode("utf-8", "replace")
    return srv, get


@test
def test_the_panel_page_carries_a_strict_content_security_policy(tmp: Path):
    """Страница панели отдаётся с CSP: скрипты свои и один встроенный с одноразовым nonce, без
    плагинов, `<base>` и чужой рамки. Раньше заголовков безопасности не было вовсе: любой
    скрипт в origin читал токен из глобальной `TOKEN` и звал `/api/run`."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    srv, get = _server(ck, tmp)
    saved = ck.i18n_catalogue
    # Строка каталога с закрывающим тегом не должна разрывать встроенный скрипт.
    ck.i18n_catalogue = lambda lang: {"strings": {"x.evil": "</script><script>alert(1)</script>"}}
    try:
        code, head, body = get("/")
        code2, head2, _ = get("/")
    finally:
        ck.i18n_catalogue = saved
        srv.shutdown()
    assert code == 200 and code2 == 200
    csp = head.get("content-security-policy", "")
    assert csp, "страница без Content-Security-Policy"
    directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce, csp
    # Кроме своих файлов и nonce — только хэш встроенного скрипта значков редактора (его
    # редактор создаёт сам, без nonce), посчитанный с файла вендора.
    import base64
    import hashlib
    icons = (COCKPIT / "vendor/vditor/dist/js/icons/ant.js").read_bytes()
    want = "'sha256-%s'" % base64.b64encode(hashlib.sha256(icons).digest()).decode()
    assert directives["script-src"] == ["'self'", f"'nonce-{nonce.group(1)}'", want], \
        f"script-src пропускает лишнее: {directives['script-src']}"
    assert "'unsafe-inline'" not in directives["script-src"] and "'unsafe-eval'" not in csp
    assert directives["object-src"] == ["'none'"] and directives["base-uri"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert head.get("x-frame-options") == "DENY" and head.get("x-content-type-options") == "nosniff"
    # Каждый скрипт страницы несёт тот же nonce, nonce у каждого ответа свой.
    tags = re.findall(r"<script\b[^>]*>", body)
    assert tags and all(f'nonce="{nonce.group(1)}"' in t for t in tags), tags
    assert nonce.group(1) != re.search(r"'nonce-([^']+)'", head2["content-security-policy"]).group(1)
    assert "</script><script>alert(1)" not in body and "\\u003c/script>" in body, \
        "«<» в каталоге строк не экранирован: строка закрывает скрипт страницы"


@test
def test_the_panel_script_has_no_inline_handlers_a_strict_policy_would_block(_t):
    """CSP без `unsafe-inline` для скриптов молча глушит `onclick="…"` и `javascript:`: код
    панели собирает разметку строками, и такие обработчики туда проникают незаметно."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    pages = [ck.ui_source()]
    for f in (COCKPIT / "modules").rglob("*.js"):
        pages.append(f.read_text(encoding="utf-8"))
    for text in pages:
        bad = re.findall(r"""\bon[a-z]{3,}\s*=\s*\\?["'][^"']*["']""", text)
        assert not bad, f"встроенный обработчик в разметке: {bad[:3]}"
        assert "javascript:" not in text, "ссылка javascript: в коде панели"


@test
def test_mermaid_in_the_editor_is_rendered_strictly(tmp: Path):
    """Редактор рисует mermaid с `securityLevel:"loose"` — `click A href "javascript:…"` в чужой
    схеме из зеркала исполнялся по клику. Файл вендора не правим: при отдаче режим меняется
    на `strict`, а страница берёт библиотеку по новому адресу (кэш на сутки не держит старую)."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    disk = (COCKPIT / "vendor/vditor/dist/index.min.js").read_text(encoding="utf-8")
    assert disk.count('securityLevel:"loose"') == 1, \
        "вендор сменился: пересмотрите правку VENDOR_PATCHES (где теперь задан режим mermaid)"
    srv, get = _server(ck, tmp)
    try:
        code, head, body = get("/vendor/vditor/dist/index.min.js?s=1")
    finally:
        srv.shutdown()
    assert code == 200
    assert 'securityLevel:"strict"' in body and 'securityLevel:"loose"' not in body, \
        "редактор отдан с mermaid в режиме loose"
    assert head.get("x-content-type-options") == "nosniff"
    assert body.encode("utf-8") == disk.encode("utf-8").replace(b'securityLevel:"loose"',
                                                                b'securityLevel:"strict"'), \
        "правка при отдаче задела лишнее"
    assert "/vendor/vditor/dist/index.min.js?s=1" in ck.ui_source(), \
        "страница берёт редактор по старому адресу: кэш вернёт mermaid в режиме loose"


@test
def test_reports_are_served_in_a_sandbox_without_the_panel_origin(tmp: Path):
    """Отчёт — чужой по составу HTML (имена из Jira, график с CDN): он отдаётся в песочнице без
    `allow-same-origin`, с закрытой сетью, чтобы скрипт отчёта не мог звать API панели."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    (tmp / "out.html").write_text("<html>отчёт</html>", encoding="utf-8")
    saved = (ck.find_projects, ck.report_state, ck.report_version_path)
    ck.find_projects = lambda roots: [{"path": str(tmp)}]
    ck.report_state = lambda p: {"reports": [{"id": "analyst",
                                              "output": {"exists": True, "path": "out.html"}}]}
    ck.report_version_path = lambda p, i, stamp: str(tmp / "out.html")
    srv, get = _server(ck, tmp)
    try:
        base = f"/api/report/file?project={tmp}&id=analyst"
        now = get(base)
        old = get(base + "&stamp=20260101-000000")
    finally:
        srv.shutdown()
        ck.find_projects, ck.report_state, ck.report_version_path = saved
    for code, head, body in (now, old):
        assert code == 200 and "отчёт" in body
        csp = head.get("content-security-policy", "")
        assert csp.startswith("sandbox allow-scripts"), f"отчёт без песочницы: {csp!r}"
        assert "allow-same-origin" not in csp and "allow-top-navigation" not in csp, csp
        assert "connect-src 'none'" in csp and "default-src 'none'" in csp, csp
        assert "'self'" not in csp, "отчёту разрешён обмен со своим адресом, то есть с панелью"
        assert head.get("x-frame-options") == "DENY"


@test
def test_the_report_page_escapes_people_names_before_putting_them_into_markup(_t):
    """Отображаемое имя в Jira задаёт сам пользователь чужой системы. Шаблон отчёта вставлял его
    в `innerHTML` как есть: имя с тегом становилось кодом в странице отчёта."""
    src = (KIT / "reports/analyst/make_extended.py").read_text(encoding="utf-8")
    assert "const esc = s =>" in src, "в шаблоне отчёта нет экранирования"
    for raw in ("<span>${r}</span>", "<span>${p}${alias}</span>", "<span>${w}</span>",
                "<b>${emp}</b>", "<td>${d.role}</td>", "<b>${fio}</b>", 'title="${t}"',
                "(${MERGED[p].aliases.join(', ')})"):
        assert raw not in src, f"имя идёт в разметку без esc(): {raw}"


# --------------------------------------------------------------------------- токен панели


def _http(srv):
    """Запрос к настоящему серверу: (код, заголовки, тело). Заголовки — как передали."""
    import http.client

    def call(method: str, path: str, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        raw = r.read().decode("utf-8", "replace")
        return r.status, {k.lower(): v for k, v in r.getheaders()}, raw
    return call


def _json_of(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return {}


@test
def test_the_panel_page_carries_no_token_and_gives_it_for_a_one_time_code(tmp: Path):
    """Страница панели отдаётся любому на петле, поэтому токена в ней нет: он выдаётся в обмен
    на код из адреса, который живёт недолго и расходуется первым обменом. Раньше токен лежал в
    теле `GET /`, и любой процесс или пользователь той же машины забирал его одним запросом."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    srv, get = _server(ck, tmp)
    call = _http(srv)
    js = {"Content-Type": "application/json"}
    try:
        code, _head, body = get("/")
        assert code == 200 and ck.TOKEN not in body and "__AURORA_TOKEN__" not in body, \
            "страница отдана вместе с токеном"
        # код из адреса печатной ссылки — один раз
        c1 = ck.issue_launch_code()
        st, _h, raw = call("POST", "/api/session", js, json.dumps({"code": c1}))
        assert st == 200 and _json_of(raw).get("token") == ck.TOKEN, (st, raw)
        st, _h, raw = call("POST", "/api/session", js, json.dumps({"code": c1}))
        assert st == 403 and _json_of(raw).get("code") == "session", "код сработал дважды"
        # чужой код и пустое тело
        for bad in ({"code": "выдуманный"}, {}, {"code": None}):
            st, _h, raw = call("POST", "/api/session", js, json.dumps(bad))
            assert st == 403 and ck.TOKEN not in raw, bad
        # истёкший код
        c2 = ck.issue_launch_code()
        ck._LAUNCH_CODES[c2] = time.monotonic() - 1
        st, _h, _r = call("POST", "/api/session", js, json.dumps({"code": c2}))
        assert st == 403, "истёкший код принят"
        # чужая страница не может потратить код и забрать токен: источник проверяется до кода
        c3 = ck.issue_launch_code()
        st, _h, raw = call("POST", "/api/session", {**js, "Origin": "http://evil.example"},
                           json.dumps({"code": c3}))
        assert st == 403 and ck.TOKEN not in raw, "чужой Origin получил токен"
        st, _h, raw = call("POST", "/api/session", {"Content-Type": "text/plain"},
                           json.dumps({"code": c3}))
        assert st == 415 and ck.TOKEN not in raw, "тело text/plain принято как JSON"
        st, _h, raw = call("POST", "/api/session", {**js, "Origin": f"http://localhost:{srv.server_address[1]}"},
                           json.dumps({"code": c3}))
        assert st == 200, "свой Origin и целый код отвергнуты — отказы выше потратили код"
        # новая запись выдаётся уже вошедшему, тот же код чужому не годится
        st, _h, raw = call("POST", "/api/launch", {**js, "X-Aurora-Token": ck.TOKEN}, "{}")
        fresh = _json_of(raw).get("code")
        assert st == 200 and fresh and fresh in ck._LAUNCH_CODES
        st, _h, _r = call("POST", "/api/launch", js, "{}")
        assert st == 403, "код входа выдан без токена"
    finally:
        srv.shutdown()


@test
def test_the_token_travels_only_in_a_header_and_old_tabs_are_told_to_reload(tmp: Path):
    """Токен в строке адреса не принимается — даже верный; старая вкладка и старый скрипт
    получают понятное «обновите страницу», а не молчаливый обрыв. Запрос с чужого Origin или
    Sec-Fetch-Site отклоняется и с верным токеном; вендоренная статика и код разделов — без токена."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    srv, _get = _server(ck, tmp)
    call = _http(srv)
    port = srv.server_address[1]
    auth = {"X-Aurora-Token": ck.TOKEN}
    try:
        st, _h, raw = call("GET", "/api/ping", auth)
        assert st == 200 and _json_of(raw).get("app") == "aurora-cockpit"
        st, _h, raw = call("GET", f"/api/state?t={ck.TOKEN}")
        err = _json_of(raw)
        assert st == 403 and err.get("code") == "session" and "обновите страницу" in err["error"], raw
        assert "X-Aurora-Token" in err["error"], "скрипту не сказано, что передавать"
        st, _h, raw = call("GET", "/api/ping?lang=en&t=old")
        assert st == 403 and "reload the page" in _json_of(raw)["error"], raw
        st, _h, raw = call("GET", "/api/ping", {"X-Aurora-Token": "прежний-токен".encode("utf-8").decode("latin-1")})
        err = _json_of(raw)
        assert st == 403 and err.get("code") == "session" and "обновите страницу" in err["error"], raw
        st, _h, raw = call("GET", "/api/ping")
        assert st == 403 and _json_of(raw).get("code") == "session", "без токена не отказано"
        # источник запроса
        for extra in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                      {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            st, _h, raw = call("GET", "/api/ping", {**auth, **extra})
            assert st == 403 and _json_of(raw).get("code") == "origin", (extra, st, raw)
        for extra in ({"Origin": f"http://127.0.0.1:{port}"}, {"Origin": f"http://localhost:{port}"},
                      {"Sec-Fetch-Site": "same-origin"}):
            st, _h, _r = call("GET", "/api/ping", {**auth, **extra})
            assert st == 200, (extra, st)
        # статика и код разделов — без токена (браузер грузит их сам, заголовка не приложить)
        st, _h, _r = call("GET", "/modules/reports/view.js")
        assert st == 200, "код раздела требует токен"
        st, head, _r = call("GET", "/api/ping", auth)
        assert head.get("referrer-policy") == "no-referrer", "код входа может уйти в Referer"
    finally:
        srv.shutdown()


@test
def test_mutating_requests_need_json_and_the_body_is_limited(tmp: Path):
    """POST принимает только `application/json`: тело `text/plain` браузер шлёт чужой странице
    без предварительной проверки, и без этого условия оно проходило как JSON. Тело больше
    предела отклоняется до чтения."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    srv, _get = _server(ck, tmp)
    call = _http(srv)
    auth = {"X-Aurora-Token": ck.TOKEN}
    try:
        st, _h, raw = call("POST", "/api/job/stop", {**auth, "Content-Type": "text/plain"}, "{}")
        assert st == 415, f"text/plain принят: {st} {raw}"
        st, _h, _r = call("POST", "/api/job/stop", auth, "{}")
        assert st == 415, "без Content-Type принято"
        st, _h, raw = call("POST", "/api/job/stop", {**auth, "Content-Type": "application/json; charset=utf-8"}, "{}")
        assert st == 200, (st, raw)
        st, _h, _r = call("POST", "/api/job/stop", {**auth, "Content-Type": "application/json",
                                                   "Origin": "http://evil.example"}, "{}")
        assert st == 403, "чужой Origin с верным токеном"
        st, _h, _r = call("POST", "/api/job/stop", {"Content-Type": "application/json"}, "{}")
        assert st == 403, "POST без токена"
        st, _h, _r = call("POST", "/api/job/stop", {**auth, "Content-Type": "application/json",
                                                   "Content-Length": str(ck.MAX_BODY + 1)}, "{}")
        assert st == 413, f"тело сверх предела не отклонено: {st}"
        st, _h, _r = call("POST", "/api/job/stop", {**auth, "Content-Type": "application/json"}, "[1]")
        assert st == 400, "массив вместо объекта должен отклоняться, а не падать"
    finally:
        srv.shutdown()


@test
def test_running_servers_and_rebuilding_the_graph_are_not_get_requests(tmp: Path):
    """Запуск чужих серверов MCP и пересчёт графа — действия: GET их не выполняет, POST выполняет."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    AC = importlib.import_module("agent_core")
    saved = (ck.find_projects, ck.graph_state, AC.mcp_probe)
    seen = []
    ck.find_projects = lambda roots: [{"path": str(tmp)}]
    ck.graph_state = lambda project, rebuild=False: seen.append(("graph", rebuild)) or {"nodes": []}
    AC.mcp_probe = lambda _x: seen.append(("probe",)) or {"servers": {}}
    srv, _get = _server(ck, tmp)
    call = _http(srv)
    auth = {"X-Aurora-Token": ck.TOKEN}
    js = {**auth, "Content-Type": "application/json"}
    try:
        st, _h, _r = call("GET", "/api/mcp/probe", auth)
        assert st == 405 and not seen, "GET запустил серверы"
        st, _h, _r = call("GET", f"/api/graph?project={tmp}&rebuild=1", auth)
        assert st == 405 and not seen, "GET пересчитал граф"
        st, _h, _r = call("GET", f"/api/graph?project={tmp}", auth)
        assert st == 200 and seen == [("graph", False)], seen
        seen.clear()
        st, _h, _r = call("POST", "/api/mcp/probe", js, "{}")
        assert st == 200 and seen == [("probe",)], seen
        seen.clear()
        st, _h, _r = call("POST", "/api/graph", js, json.dumps({"project": str(tmp), "rebuild": True}))
        assert st == 200 and seen == [("graph", True)], seen
        st, _h, _r = call("POST", "/api/graph", js, json.dumps({"project": "/нет/такого", "rebuild": True}))
        assert st == 400 and seen == [("graph", True)], "чужой путь принят"
    finally:
        srv.shutdown()
        ck.find_projects, ck.graph_state, AC.mcp_probe = saved


@test
def test_a_report_link_ticket_opens_exactly_one_address_once(tmp: Path):
    """Отчёт открывается в новой вкладке ссылкой, заголовка у которой нет. Токен в такую ссылку
    не кладётся: сервер выдаёт билет на один адрес и одно открытие, а отчёт всё так же идёт в
    песочнице."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    (tmp / "out.html").write_text("<html>отчёт</html>", encoding="utf-8")
    saved = (ck.find_projects, ck.report_state, ck.report_version_path)
    ck.find_projects = lambda roots: [{"path": str(tmp)}]
    ck.report_state = lambda p: {"reports": [{"id": "analyst",
                                              "output": {"exists": True, "path": "out.html"}}]}
    ck.report_version_path = lambda p, i, stamp: str(tmp / "out.html")
    srv, _get = _server(ck, tmp)
    call = _http(srv)
    auth = {"X-Aurora-Token": ck.TOKEN}
    js = {**auth, "Content-Type": "application/json"}
    path = f"/api/report/file?project={tmp}&id=analyst"
    try:
        st, _h, raw = call("POST", "/api/link", js, json.dumps({"path": path}))
        url = _json_of(raw).get("url", "")
        assert st == 200 and url.startswith(path + "&c=") and ck.TOKEN not in url, raw
        st, head, body = call("GET", url)                  # без заголовка токена — по билету
        assert st == 200 and "отчёт" in body and head.get("content-security-policy", "").startswith("sandbox"), (st, body)
        st, _h, _r = call("GET", url)
        assert st == 403, "билет сработал дважды"
        # билет — на этот адрес: подмена запроса и чужой путь не проходят
        url = _json_of(call("POST", "/api/link", js, json.dumps({"path": path}))[2])["url"]
        st, _h, _r = call("GET", url.replace("id=analyst", "id=other"))
        assert st == 403, "билет подошёл к другому адресу"
        ticket = url.rsplit("c=", 1)[1]
        st, _h, _r = call("GET", f"/api/state?c={ticket}")
        assert st == 403, "билет открыл другой маршрут"
        st, _h, _r = call("POST", "/api/link", js, json.dumps({"path": "/api/state"}))
        assert st == 400, "билет выдан на маршрут, которому он не нужен"
        st, _h, _r = call("POST", "/api/link", js, json.dumps({"path": "//evil.example/api/report/file"}))
        assert st == 400, "билет выдан на чужой адрес"
        st, _h, _r = call("POST", "/api/link", {"Content-Type": "application/json"}, json.dumps({"path": path}))
        assert st == 403, "билет выдан без токена"
        # истёкший
        url = _json_of(call("POST", "/api/link", js, json.dumps({"path": path}))[2])["url"]
        ticket = url.rsplit("c=", 1)[1]
        exp, bound = ck._LINK_TICKETS[ticket]
        ck._LINK_TICKETS[ticket] = (time.monotonic() - 1, bound)
        st, _h, _r = call("GET", url)
        assert st == 403, "истёкший билет принят"
    finally:
        srv.shutdown()
        ck.find_projects, ck.report_state, ck.report_version_path = saved


@test
def test_the_session_file_is_private_from_the_first_byte_and_has_no_token_in_the_address(tmp: Path):
    """Файл сессии создаётся сразу с правами владельца (раньше — «записать, потом chmod», и на это
    время токен читали все), а в адресе токена нет; повторный вход в работающую панель идёт по
    свежему коду, а не по адресу с токеном."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    old = (ck.SESSION, os.chmod)
    ck.SESSION = str(tmp / "session.json")
    umask = os.umask(0)
    os.chmod = lambda *a, **k: None          # поздний chmod не должен быть тем, что спасает
    try:
        ck.write_session(8787)
    finally:
        os.chmod = old[1]
        os.umask(umask)
        ck.SESSION = old[0]
    path = tmp / "session.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["token"] == ck.TOKEN and row["auth"] == "code" and row["port"] == 8787, row
    assert "?" not in row["url"] and ck.TOKEN not in row["url"], "токен в адресе файла сессии"
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600, oct(path.stat().st_mode)
    assert not list(tmp.glob("session.json.*")), "временный файл остался"

    # повторный запуск: новая панель отдаёт свежий код, прежняя — свой адрес как был
    srv, _get = _server(ck, tmp)
    try:
        sess = {"port": srv.server_address[1], "token": ck.TOKEN, "auth": "code",
                "url": f"http://127.0.0.1:{srv.server_address[1]}/"}
        assert ck.alive(sess["url"], ck.TOKEN) and not ck.alive(sess["url"], "чужой")
        url = ck.reopen_url(sess)
        code = url.rsplit("?c=", 1)[1]
        assert url.startswith(f"http://127.0.0.1:{srv.server_address[1]}/?c=") and ck.TOKEN not in url, url
        assert ck.take_launch_code(code) and not ck.take_launch_code(code)
        legacy = {"port": 1, "url": "http://127.0.0.1:1/?t=abc"}
        assert ck.reopen_url(legacy) == legacy["url"] and ck.session_token(legacy) == "abc"
        assert ck.reopen_url({**sess, "port": 9}) == "", "отвечает не та панель, а адрес выдан"
    finally:
        srv.shutdown()


@test
def test_the_page_script_takes_the_token_for_a_code_and_never_puts_it_in_an_address(tmp: Path):
    """Страница под node со стабами: код из адреса меняется на токен и стирается из адреса,
    токен идёт заголовком, отказ «сессия» показывает одно окно «обновите страницу» без потока
    тостов, ссылка на отчёт берёт билет. Ни одного адреса с токеном (`?t=`) в коде панели."""
    ui = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    for rel in ("cockpit/ui/panel.js", *[str(p.relative_to(KIT)) for p in (KIT / "cockpit/modules").rglob("*.js")]):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "encodeURIComponent(TOKEN)" not in text and "encodeURIComponent(ctx.token)" not in text, \
            f"{rel}: токен снова в адресе"
    node = shutil.which("node")
    if not node:
        return
    head = ui[:ui.index("const UI_VERSION")]
    body = ui[ui.index("const tick = s =>"):ui.index("function toast(")]
    harness = """
const calls = [], toasts = [], store = {}, nodes = [];
let search = "", answers = {};
const location = {get search(){ return search; }, pathname: "/", hash: "", reload(){ calls.push("reload"); }};
const history = {replaceState(_a, _b, u){ search = ""; calls.push("replace:" + u); }};
const sessionStorage = {getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; },
                        removeItem: k => { delete store[k]; }};
const S = {lang: "ru"};
const t = k => k;
const $ = sel => nodes.find(n => "#" + n.id === sel) || (sel === "body" ? body : null);
const body = {append: n => nodes.push(n)};
const el = (tag, attrs = {}) => ({tag, id: attrs.id});
const toast = (m, k) => toasts.push([m, k]);
const window = {open(u){ calls.push("open:" + u); return w; }};
const w = {opener: 1, location: {set href(v){ calls.push("goto:" + v); }}, close(){ calls.push("close"); }};
globalThis.fetch = async (url, opts = {}) => {
  calls.push([opts.method || "GET", url, opts.headers && opts.headers["X-Aurora-Token"] || "", opts.body || ""]);
  const a = answers[url] || {status: 200, json: {}};
  return {ok: a.status < 400, status: a.status, json: async () => a.json};
};
HEAD
BODY
const out = {};
// 1. вход по коду
search = "?c=CODE1";
answers = {"/api/session": {status: 200, json: {token: "T-1"}}, "/api/ping": {status: 200, json: {}}};
out.byCode = {ok: await acquireSession(), token: TOKEN, saved: store[SESSION_KEY], calls: calls.splice(0)};
// 2. перезагрузка вкладки: адрес пуст, токен в sessionStorage
TOKEN = ""; search = "";
out.reload = {ok: await acquireSession(), token: TOKEN, calls: calls.splice(0)};
// 3. запрос идёт заголовком и без ?t=
answers = {"/api/state?x=1&lang=en": {status: 200, json: {a: 1}}};
S.lang = "en";
await api("/api/state?x=1");
out.header = calls.splice(0);
// 4. токен отвергнут сервером: одно окно, без тостов
S.lang = "ru";
answers = {"/api/state": {status: 403, json: {error: "сессия", code: "session"}}};
await api("/api/state"); await api("/api/state");
out.gate = {nodes: nodes.filter(n => n.id === "authGate").length, toasts: toasts.length, token: TOKEN, saved: store[SESSION_KEY] ?? null};
calls.length = 0;
// 5. код не подошёл — окно, токена нет
nodes.length = 0; TOKEN = ""; search = "?c=BAD"; delete store[SESSION_KEY];
answers = {"/api/session": {status: 403, json: {error: "нет", code: "session"}}};
out.badCode = {ok: await acquireSession(), token: TOKEN, gate: nodes.some(n => n.id === "authGate"), calls: calls.splice(0)};
// 6. старая закладка с ?t=: токен берём, из адреса стираем, проверяем
nodes.length = 0; TOKEN = ""; search = "?t=OLD";
answers = {"/api/ping": {status: 200, json: {}}};
out.legacy = {ok: await acquireSession(), token: TOKEN, calls: calls.splice(0)};
// 7. ссылка на отчёт: вкладка сразу, билет, переход
TOKEN = "T-1";
answers = {"/api/link": {status: 200, json: {url: "/api/report/file?id=a&c=TICKET"}}};
await openLink("/api/report/file?id=a");
out.link = {calls: calls.splice(0), opener: w.opener};
answers = {"/api/link": {status: 400, json: {error: "нет"}}};
await openLink("/api/state");
out.refused = calls.splice(0).filter(c => typeof c === "string");
console.log(JSON.stringify(out));
""".replace("HEAD", head.replace("let TOKEN", "var TOKEN").replace("const SESSION_KEY", "var SESSION_KEY")).replace("BODY", body)
    src = tmp / "session.mjs"
    src.write_text(harness, encoding="utf-8")
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1200]
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["byCode"]["ok"] and r["byCode"]["token"] == "T-1" and r["byCode"]["saved"] == "T-1", r["byCode"]
    assert "replace:/" in r["byCode"]["calls"], "код остался в адресе"
    assert r["byCode"]["calls"][0] == ["POST", "/api/session", "", '{"code":"CODE1"}'], r["byCode"]["calls"]
    assert r["reload"]["ok"] and r["reload"]["token"] == "T-1", r["reload"]
    assert r["header"] == [["GET", "/api/state?x=1&lang=en", "T-1", ""]], r["header"]
    assert r["gate"] == {"nodes": 1, "toasts": 0, "token": "", "saved": None}, r["gate"]
    assert not r["badCode"]["ok"] and r["badCode"]["token"] == "" and r["badCode"]["gate"], r["badCode"]
    assert r["legacy"]["ok"] and r["legacy"]["token"] == "OLD" and "replace:/" in r["legacy"]["calls"], r["legacy"]
    link = r["link"]["calls"]
    assert link[0] == "open:about:blank" and r["link"]["opener"] is None, link
    assert link[-1] == "goto:/api/report/file?id=a&c=TICKET" and "TICKET" not in json.dumps(link[1:-1]), link
    assert r["refused"] == ["open:about:blank", "close"], r["refused"]


# --------------------------------------------------------------------------- доработка BL-SEC-03


@test
def test_the_origin_is_compared_with_the_host_header_so_a_port_forward_still_works(tmp: Path):
    """При пробросе порта (`ssh -L 9000:127.0.0.1:8787`) страница открыта на другом порту, и её
    Origin — с этим портом. Сверка с портом сервера отвечала 403 на каждый POST, включая вход.
    Теперь Origin сверяется с Host: свой порт проходит, чужой порт петли (другая программа на
    той же машине) — нет."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    srv, _get = _server(ck, tmp)
    call = _http(srv)
    js = {"Content-Type": "application/json"}
    try:
        for host, origin, want in (
                ("localhost:9000", "http://localhost:9000", 200),
                ("127.0.0.1:9000", "http://127.0.0.1:9000", 200),
                ("[::1]:9000", "http://[::1]:9000", 200),
                ("localhost:9000", "http://localhost:3000", 403),          # чужой порт петли
                ("localhost:9000", f"http://localhost:{srv.server_address[1]}", 403),
                ("localhost:9000", "http://evil.example:9000", 403),
                ("localhost:9000", "https://localhost:9000", 403),
                ("localhost:9000", "http://localhost:9000/x", 403),
                ("localhost", "http://localhost", 200)):                    # порт по умолчанию
            st, _h, raw = call("GET", "/api/ping",
                               {"Host": host, "Origin": origin, "X-Aurora-Token": ck.TOKEN})
            assert st == want, (host, origin, st, raw)
            if want == 403:
                assert _json_of(raw).get("code") == "origin", (host, origin, raw)
        # вход и действие через проброс: код меняется на токен, POST с токеном проходит
        fwd = {**js, "Host": "localhost:9000", "Origin": "http://localhost:9000"}
        st, _h, raw = call("POST", "/api/session", fwd, json.dumps({"code": ck.issue_launch_code()}))
        assert st == 200 and _json_of(raw).get("token") == ck.TOKEN, (st, raw)
        st, _h, raw = call("POST", "/api/launch", {**fwd, "X-Aurora-Token": ck.TOKEN}, "{}")
        assert st == 200 and _json_of(raw).get("code"), (st, raw)
        # чужая страница на соседнем порту по-прежнему не получает токен
        st, _h, raw = call("POST", "/api/session",
                           {**js, "Host": "localhost:9000", "Origin": "http://localhost:3000"},
                           json.dumps({"code": ck.issue_launch_code()}))
        assert st == 403 and ck.TOKEN not in raw, (st, raw)
    finally:
        srv.shutdown()


@test
def test_a_report_link_ticket_survives_the_browser_normalising_the_address(tmp: Path):
    """Браузер приводит адрес к своему виду: апостроф в запросе уходит как `%27`, а страница
    отправила путь с апострофом как есть. Билет сверялся строкой, и «Открыть» у отчёта проекта
    с таким путём (`/Users/O'Brien/…`) открывал вкладку с ошибкой. Сверка теперь по разобранному
    адресу. Повторное открытие отвечает понятно, а не «откройте адрес из консоли»."""
    _path()
    from urllib.parse import quote
    ck = importlib.import_module("aurora_cockpit")
    proj = tmp / "O'Brien proj x"
    proj.mkdir()
    (proj / "out.html").write_text("<html>отчёт</html>", encoding="utf-8")
    saved = (ck.find_projects, ck.report_state, ck.report_version_path)
    ck.find_projects = lambda roots: [{"path": str(proj)}]
    ck.report_state = lambda p: {"reports": [{"id": "analyst",
                                              "output": {"exists": True, "path": "out.html"}}]}
    ck.report_version_path = lambda p, i, stamp: str(proj / "out.html")
    srv, _get = _server(ck, tmp)
    call = _http(srv)
    js = {"X-Aurora-Token": ck.TOKEN, "Content-Type": "application/json"}
    # как строит адрес страница: encodeURIComponent оставляет апостроф как есть
    path = f"/api/report/file?project={quote(str(proj), safe=chr(39))}&id=analyst"
    assert "'" in path and " " not in path
    try:
        url = _json_of(call("POST", "/api/link", js, json.dumps({"path": path}))[2])["url"]
        browser = url.replace("'", "%27")                  # так запрос уходит из браузера
        assert browser != url
        st, head, body = call("GET", browser)
        assert st == 200 and "отчёт" in body, (st, body)
        assert head.get("cross-origin-resource-policy") == "same-origin", head
        # то же в другом порядке параметров и с другим написанием пробела — тот же адрес
        url = _json_of(call("POST", "/api/link", js, json.dumps({"path": path}))[2])["url"]
        ticket = url.rsplit("c=", 1)[1]
        other = f"/api/report/file?id=analyst&project={quote(str(proj), safe='')}&c={ticket}"
        st, _h, _b = call("GET", other)
        assert st == 200, "тот же адрес в другой записи не принят"
        # чужой проект или параметр — отказ
        url = _json_of(call("POST", "/api/link", js, json.dumps({"path": path}))[2])["url"]
        st, _h, _b = call("GET", url.replace("analyst", "analyst2"))
        assert st == 403
        # повторное открытие: сообщение про отчёт, а не про консоль, в том числе по-английски
        st, _h, raw = call("GET", browser)
        err = _json_of(raw)
        assert st == 403 and err.get("code") == "link" and "консол" not in err["error"] \
            and "«Отчёты»" in err["error"], raw
        st, _h, raw = call("GET", browser + "&lang=en")
        assert "Reports" in _json_of(raw)["error"] and "консол" not in raw, raw
        # без билета совет «откройте отчёт ещё раз» никуда не ведёт: ответ про токен и сессию
        base = f"/api/report/file?project={quote(str(proj), safe='')}&id=analyst"
        st, _h, raw = call("GET", base + f"&t={ck.TOKEN}")
        assert st == 403 and _json_of(raw).get("code") == "session" \
            and "обновите страницу" in _json_of(raw)["error"], raw
        st, _h, raw = call("GET", base)
        assert st == 403 and _json_of(raw).get("code") == "session" \
            and "«Отчёты»" not in _json_of(raw)["error"], raw
        st, _h, raw = call("GET", base, {"X-Aurora-Token": "old-token"})
        assert st == 403 and _json_of(raw).get("code") == "session" \
            and "закончилась" in _json_of(raw)["error"], raw
        # билет не выдаётся на адрес, где уже есть параметр `c`
        st, _h, _r = call("POST", "/api/link", js, json.dumps({"path": path + "&c=x"}))
        assert st == 400
    finally:
        srv.shutdown()
        ck.find_projects, ck.report_state, ck.report_version_path = saved


@test
def test_the_browser_gets_a_local_redirect_file_and_not_the_code_in_its_arguments(tmp: Path):
    """Адрес с кодом входа, переданный `webbrowser.open`, лежит в `argv` программы «открыть» и
    виден другому пользователю машины. Браузеру отдаётся путь к файлу в закрытой папке; в файле —
    переход на адрес панели. Проба идёт во временной папке теста."""
    _path()
    import tempfile
    ck = importlib.import_module("aurora_cockpit")
    opened = []
    old = (ck.webbrowser.open, tempfile.tempdir, ck.OPEN_FILE_TTL)
    stale = tmp / (ck.OPEN_PREFIX + "stale")
    stale.mkdir()
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    ck.webbrowser.open = lambda u, *a, **k: opened.append(u) or True
    saved_env = os.environ.pop("AURORA_COCKPIT_OPEN", None)
    saved_direct = ck.direct_open
    ck.direct_open = lambda: False         # проба идёт и в WSL, где выбран прямой путь
    tempfile.tempdir = str(tmp)
    ck.OPEN_FILE_TTL = 0.3                 # таймер уборки не должен жить дольше теста
    try:
        url = "http://127.0.0.1:8787/?c=SECRETCODE123"
        assert ck.open_in_browser(url) is True
        assert len(opened) == 1 and opened[0].startswith("file://") and "SECRETCODE123" not in opened[0], opened
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        page = Path(url2pathname(urlparse(opened[0]).path))     # на Windows адрес — file:///C:/…
        assert page.is_file() and url in page.read_text(encoding="utf-8")
        assert "location.replace" in page.read_text(encoding="utf-8")
        if os.name == "posix":
            assert (page.stat().st_mode & 0o777) == 0o600, oct(page.stat().st_mode)
            assert (page.parent.stat().st_mode & 0o777) == 0o700, oct(page.parent.stat().st_mode)
        assert not stale.exists(), "прежняя папка с кодом не убрана"
        for _ in range(60):                # короткий таймер уборки
            if not page.exists():
                break
            time.sleep(0.1)
        assert not page.exists(), "файл с кодом остался после срока кода"
    finally:
        ck.webbrowser.open, tempfile.tempdir, ck.OPEN_FILE_TTL = old
        ck.direct_open = saved_direct
        if saved_env is not None:
            os.environ["AURORA_COCKPIT_OPEN"] = saved_env


@test
def test_the_browser_opens_the_address_directly_where_a_local_file_does_not_reach_it(tmp: Path):
    """Браузер из snap (свой /tmp) и браузер Windows при панели в WSL не видят файл-переход.
    `AURORA_COCKPIT_OPEN=direct` и WSL возвращают прямой вызов с адресом; ничего не пишется в
    папку для файлов. Второй случай — запрет вернуть прямой вызов вне `open_in_browser`: код входа
    в аргументах программы «открыть» — то, ради чего сделан файл-переход."""
    _path()
    import ast
    import tempfile
    ck = importlib.import_module("aurora_cockpit")
    opened = []
    old = (ck.webbrowser.open, tempfile.tempdir, os.environ.get("AURORA_COCKPIT_OPEN"),
           ck.platform.release, ck.sys.platform)
    ck.webbrowser.open = lambda u, *a, **k: opened.append(u) or True
    tempfile.tempdir = str(tmp)
    try:
        os.environ.pop("AURORA_COCKPIT_OPEN", None)
        ck.sys.platform = "linux"
        ck.platform.release = lambda: "5.15.90.1-microsoft-standard-WSL2"
        assert ck.direct_open() is True
        ck.platform.release = lambda: "6.8.0-generic"
        assert ck.direct_open() is False
        os.environ["AURORA_COCKPIT_OPEN"] = "Direct"
        assert ck.direct_open() is True
        url = "http://127.0.0.1:8787/?c=ABC"
        assert ck.open_in_browser(url) is True and opened == [url], opened
        assert not any(tmp.iterdir()), "файл-переход создан при прямом пути"
    finally:
        ck.webbrowser.open, tempfile.tempdir, env, ck.platform.release, ck.sys.platform = old
        if env is None:
            os.environ.pop("AURORA_COCKPIT_OPEN", None)
        else:
            os.environ["AURORA_COCKPIT_OPEN"] = env
    # вызовы webbrowser.open — только внутри open_in_browser; main() зовёт именно её
    src = (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    holders = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "open" and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "webbrowser"):
                    holders.add(fn.name)
    assert holders == {"open_in_browser"}, holders
    main_src = src[src.index("\ndef main()"):]
    assert "open_in_browser(url)" in main_src and "open_in_browser, args=(url,)" in main_src, \
        "main() не открывает браузер через open_in_browser"


@test
def test_the_browser_opening_mode_follows_the_platform_unless_the_owner_chooses(tmp: Path):
    """Файл-переход `open.html` на macOS и Windows открывает программу, назначенную на `.html`
    (часто редактор), а не браузер. Поэтому там по умолчанию прямое открытие, на Linux — файл;
    `AURORA_COCKPIT_OPEN=file|direct` перекрывает платформу. Браузер не запускается: подменены
    `sys.platform`, `platform.release` и `webbrowser.open`."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    old = (os.environ.get("AURORA_COCKPIT_OPEN"), ck.platform.release, ck.sys.platform,
           ck.webbrowser.open)
    opened = []
    ck.webbrowser.open = lambda u, *a, **k: opened.append(u) or True
    ck.platform.release = lambda: "6.8.0-generic"
    try:
        os.environ.pop("AURORA_COCKPIT_OPEN", None)
        for plat, want in (("darwin", True), ("win32", True), ("linux", False)):
            ck.sys.platform = plat
            assert ck.direct_open() is want, (plat, want)
        ck.sys.platform = "darwin"
        os.environ["AURORA_COCKPIT_OPEN"] = " File "
        assert ck.direct_open() is False, "file должен вернуть файл-переход на macOS"
        ck.sys.platform = "linux"
        os.environ["AURORA_COCKPIT_OPEN"] = "direct"
        assert ck.direct_open() is True
        os.environ["AURORA_COCKPIT_OPEN"] = "мусор"
        assert ck.direct_open() is False, "незнакомое значение не меняет выбор платформы"
        os.environ.pop("AURORA_COCKPIT_OPEN", None)
        ck.sys.platform = "darwin"
        url = "http://127.0.0.1:8787/?c=ABC"
        assert ck.open_in_browser(url) is True and opened == [url], opened
    finally:
        env, ck.platform.release, ck.sys.platform, ck.webbrowser.open = old
        if env is None:
            os.environ.pop("AURORA_COCKPIT_OPEN", None)
        else:
            os.environ["AURORA_COCKPIT_OPEN"] = env


@test
def test_a_pre_update_tab_polling_ping_with_the_token_in_the_address_sees_the_panel_come_up(tmp: Path):
    """Страница до 1.171 после «Обновить кит» опрашивает `/api/ping?t=<токен>`; ответ 403 вёл
    к ложному «панель не поднялась». Верный токен принимается только на GET /api/ping (там версия
    и готовность); неверный и любой другой путь — отказ."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    srv, _get = _server(ck, tmp)
    call = _http(srv)
    try:
        st, _h, raw = call("GET", f"/api/ping?t={ck.TOKEN}")
        assert st == 200 and _json_of(raw).get("app") == "aurora-cockpit" and "ready" in _json_of(raw), raw
        st, _h, raw = call("GET", "/api/ping?t=old")
        assert st == 403, raw
        st, _h, raw = call("GET", f"/api/ping?t={ck.TOKEN}", {"Origin": "http://evil.example"})
        assert st == 403 and _json_of(raw).get("code") == "origin", raw
        st, _h, raw = call("POST", f"/api/ping?t={ck.TOKEN}", {}, "{}")
        assert st != 200, raw
        for path in ("/api/state", "/api/jobs"):
            st, _h, raw = call("GET", f"{path}?t={ck.TOKEN}")
            assert st == 403, (path, raw)
    finally:
        srv.shutdown()


@test
def test_the_token_goes_to_the_loopback_port_and_never_to_the_host_from_the_session_file(tmp: Path):
    """`alive()` берёт из адреса файла сессии только порт: токен уходит на 127.0.0.1, а не на
    хост, который туда записан."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    srv, _get = _server(ck, tmp)
    try:
        port = srv.server_address[1]
        assert ck.alive(f"http://evil.example:{port}/", ck.TOKEN), "порт из адреса не использован"
        assert not ck.alive("http://evil.example/", ck.TOKEN), "адрес без порта принят"
        assert not ck.alive("не адрес", ck.TOKEN)
    finally:
        srv.shutdown()
