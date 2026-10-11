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
ENV_LOCAL = ".env.aurora" + ".local"


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


# ------------------------------------------------------------ kb_fix: планировщики композируются

def _kbf_project(tmp: Path) -> tuple:
    """Синтетический проект для `kb_fix` во временной папке: (корень, папка базы)."""
    root = tmp / "proj"
    kb = root / "AuroraKnowledgeDB"
    for sec in ("Concepts", "Processes", "Glossary", "Reference"):
        (kb / sec).mkdir(parents=True)
    return root, kb


def _kbf_run(root: Path, *flags: str):
    return subprocess.run([sys.executable, str(KIT / "scripts/kb_fix.py"), *flags, "--apply"],
                          cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def _kbf_tree(kb: Path) -> dict:
    return {str(p.relative_to(kb)).replace("\\", "/"): p.read_text(encoding="utf-8")
            for p in sorted(kb.rglob("*.md"))}


@test
def test_kb_fix_names_and_sections_compose_on_one_card(tmp: Path):
    """Карточке нужны и снятие кода из имени, и перенос в раздел своего типа: оба шага
    применяются к одному файлу.

    Раньше `plan_sections` брал исходный текст карточки, затирая запись `plan_names` (заголовок
    и синонимы), и ставил второе переименование от того же старого пути: `os.rename` падал на
    `FileNotFoundError` посреди записи. Теперь план читает свои записи и сводит два переименования
    в одно: имя — от снятия кода, папка — от раздела.
    """
    root, kb = _kbf_project(tmp)
    (kb / "Concepts" / "AC-3.4.2-Отправка.md").write_text(
        '---\ntitle: "AC-3.4.2 Отправка начислений"\naliases:\n  - "Своё"\n'
        'type: algorithm\nstatus: knowledge\n---\n\n# AC-3.4.2 Отправка начислений\n\nТело.\n',
        encoding="utf-8")
    cp = _kbf_run(root, "--names", "--sections")
    assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, cp.stderr[-600:]
    tree = _kbf_tree(kb)
    assert list(tree) == ["Processes/Отправка-начислений.md"], \
        f"карточка потеряна или раздвоилась: {list(tree)}"
    text = tree["Processes/Отправка-начислений.md"]
    assert 'title: "Отправка начислений"' in text, "правка имён затёрта правкой разделов"
    assert "type: process" in text, "тип не приведён к схемному"
    for keep in ("AC-3.4.2", "Своё"):
        assert f'"{keep}"' in text, f"синоним «{keep}» потерян"
    # Повторный прогон ничего не меняет: ремонт идемпотентен.
    again = _kbf_run(root, "--names", "--sections")
    assert again.returncode in (0, 1) and "Traceback" not in again.stderr, again.stderr[-600:]
    assert _kbf_tree(kb) == tree, "повторный прогон изменил базу"


@test
def test_kb_fix_sections_and_homoglyphs_compose_on_one_card(tmp: Path):
    """Смешанный скрипт в имени и чужой раздел у одной карточки: папка — от переноса, имя — от
    починки гомоглифов, один `rename`, и повторный прогон ничего не меняет."""
    root, kb = _kbf_project(tmp)
    mixed = "Т\u0065ма"                    # латинская «e» среди кириллицы
    (kb / "Concepts" / f"{mixed}.md").write_text(
        f'---\ntitle: "{mixed}"\ntype: process\nstatus: knowledge\n---\n\n# {mixed}\n\nТело.\n',
        encoding="utf-8")
    cp = _kbf_run(root, "--sections", "--homoglyphs")
    assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, cp.stderr[-600:]
    tree = _kbf_tree(kb)
    assert list(tree) == ["Processes/Тема.md"], list(tree)
    assert f'"{mixed}"' in tree["Processes/Тема.md"], "прежнее имя не ушло в синонимы"
    again = _kbf_run(root, "--sections", "--homoglyphs")
    assert again.returncode in (0, 1) and "Traceback" not in again.stderr, again.stderr[-600:]
    assert _kbf_tree(kb) == tree, "повторный прогон изменил базу"


@test
def test_kb_fix_homoglyphs_and_links_compose_on_a_nonexact_link(tmp: Path):
    """Смешанный скрипт в имени карточки и ссылка на неё не по точному имени (код документа
    впереди): `--all` не падает, ссылка и синоним пишутся в файл под ПРЕЖНИМ путём.

    Раньше `plan_homoglyphs` направлял индекс на новый путь — файла, которого ещё нет, а
    `plan_links` брал `cards[tpath]` и падал `KeyError` (код 3) даже без `--apply`.
    """
    root, kb = _kbf_project(tmp)
    mixed = "Р\u0065естр"                  # латинская «e» среди кириллицы: «Реестр»
    (kb / "Concepts" / f"{mixed}.md").write_text(
        f'---\ntitle: "{mixed}"\ntype: concept\nstatus: knowledge\n---\n\n# {mixed}\n\nТело.\n',
        encoding="utf-8")
    (kb / "Concepts" / "Ссылка.md").write_text(
        '---\ntitle: "Ссылка"\ntype: concept\nstatus: knowledge\n---\n\n# Ссылка\n\n'
        'См. [[AC-1.2 Реестр]].\n', encoding="utf-8")
    dry = subprocess.run([sys.executable, str(KIT / "scripts/kb_fix.py"), "--all"],
                         cwd=root, capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    assert dry.returncode in (0, 1) and "Traceback" not in dry.stderr, dry.stderr[-600:]
    cp = _kbf_run(root, "--all")
    assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, cp.stderr[-600:]
    tree = _kbf_tree(kb)
    assert sorted(tree) == ["Concepts/Реестр.md", "Concepts/Ссылка.md"], list(tree)
    assert "[[Реестр]]" in tree["Concepts/Ссылка.md"], "ссылка не переведена на новое имя"
    again = _kbf_run(root, "--all")
    assert again.returncode in (0, 1) and "Traceback" not in again.stderr, again.stderr[-600:]
    assert _kbf_tree(kb) == tree, "повторный прогон изменил базу"


@test
def test_kb_fix_conflicting_destination_leaves_the_card_in_place(tmp: Path):
    """Если свёрнутое назначение занято другой карточкой, перенос не выполняется и не падает:
    карточка остаётся под новым именем в прежнем разделе, обе карточки целы."""
    root, kb = _kbf_project(tmp)
    (kb / "Concepts" / "US-1.1-Тема.md").write_text(
        '---\ntitle: "US-1.1 Тема"\ntype: process\nstatus: knowledge\n---\n\n# US-1.1 Тема\n\nА.\n',
        encoding="utf-8")
    (kb / "Processes" / "Тема.md").write_text(
        '---\ntitle: "Тема"\ntype: process\nstatus: knowledge\n---\n\n# Тема\n\nБ.\n',
        encoding="utf-8")
    cp = _kbf_run(root, "--names", "--sections")
    assert cp.returncode in (0, 1) and "Traceback" not in cp.stderr, cp.stderr[-600:]
    tree = _kbf_tree(kb)
    assert sorted(tree) == ["Concepts/Тема.md", "Processes/Тема.md"], list(tree)
    assert "Б." in tree["Processes/Тема.md"] and "А." in tree["Concepts/Тема.md"], \
        "тело одной из карточек затёрто другой"


@test
def test_kb_fix_plan_rename_folds_compositions_and_refuses_conflicts(tmp: Path):
    """`Plan.rename`: имя от одного шага и папка от другого сводятся в одно переименование;
    два разных назначения одного источника и два источника в одно назначение — отказ."""
    sys.path.insert(0, str(SCRIPTS))
    fx = importlib.import_module("kb_fix")
    importlib.reload(fx)
    old = os.getcwd()
    os.chdir(tmp)                       # проверка занятости смотрит на диск: пустая папка
    try:
        p = fx.Plan()
        assert p.rename("K/Concepts/AC-1 Имя.md", "K/Concepts/Имя.md") == "K/Concepts/Имя.md"
        # перенос по разделу считан от исходного пути, а имя уже сменено — сводим
        assert p.rename("K/Concepts/AC-1 Имя.md", "K/Processes/AC-1 Имя.md") == "K/Processes/Имя.md"
        assert p.renames == [("K/Concepts/AC-1 Имя.md", "K/Processes/Имя.md")]
        # повтор того же назначения — не новая запись
        assert p.rename("K/Concepts/AC-1 Имя.md", "K/Processes/Имя.md") == "K/Processes/Имя.md"
        assert len(p.renames) == 1
        # оба шага меняют имя по-разному — решает человек, прежняя запись остаётся
        assert p.rename("K/Concepts/AC-1 Имя.md", "K/Concepts/Другое.md") is None
        assert p.renames == [("K/Concepts/AC-1 Имя.md", "K/Processes/Имя.md")]
        # два источника — одно назначение
        assert p.rename("K/Concepts/Иное.md", "K/Processes/Имя.md") is None
        assert len(p.renames) == 1
    finally:
        os.chdir(old)


@test
def test_kb_fix_apply_plan_skips_a_vanished_source_without_crashing(tmp: Path):
    """`apply_plan` не падает на `FileNotFoundError`: источник, которого уже нет, и второе
    переименование того же файла пропускаются со счётчиком, остальной план применяется."""
    sys.path.insert(0, str(SCRIPTS))
    fx = importlib.import_module("kb_fix")
    importlib.reload(fx)
    old = os.getcwd()
    os.chdir(tmp)
    try:
        (tmp / "a").mkdir()
        (tmp / "a" / "x.md").write_text("x", encoding="utf-8")
        (tmp / "a" / "y.md").write_text("y", encoding="utf-8")
        p = fx.Plan()
        p.renames.extend([("a/x.md", "b/x.md"), ("a/x.md", "c/x.md"),     # два назначения у одного
                          ("a/нет.md", "b/нет.md"),                      # источника уже нет
                          ("a/y.md", "b/y.md")])
        skipped = fx.apply_plan(p)
        assert skipped == 2, skipped
        assert (tmp / "b" / "x.md").is_file() and not (tmp / "c" / "x.md").exists()
        assert (tmp / "b" / "y.md").is_file(), "остальной план не применён"
    finally:
        os.chdir(old)


# ------------------------------------------------------------------ ключ ходит только к своему хосту

class _Hosts:
    """Два настоящих HTTP-сервера на 127.0.0.1 (разные порты — разные хосты для ключа).

    `a` отвечает 302 на адрес в `to` по пути `/go`, 302 на свой `/ok` по пути `/same`; оба
    пишут заголовки каждого запроса. Так видно, что реально ушло по сети."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.seen = {"a": [], "b": []}
        self.to = ""
        self.redirect_all = False       # сервер `a` на любой путь отвечает редиректом на `to`
        outer = self

        def make(name):
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    outer.seen[name].append(
                        (self.path, {k.lower(): v for k, v in self.headers.items()}))
                    if self.path in ("/go", "/same") or (name == "a" and outer.redirect_all):
                        self.send_response(302)
                        self.send_header("Location", outer.to if self.path != "/same" else "/ok")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                do_POST = do_GET

                def log_message(self, *a):
                    pass
            return H
        self.srv = {n: ThreadingHTTPServer(("127.0.0.1", 0), make(n)) for n in ("a", "b")}
        self.port = {n: s.server_address[1] for n, s in self.srv.items()}
        for s in self.srv.values():
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def url(self, name: str, path: str = "") -> str:
        return f"http://127.0.0.1:{self.port[name]}{path}"

    def close(self):
        for s in self.srv.values():
            s.shutdown()
            s.server_close()


@test
def test_redirect_to_another_host_does_not_carry_the_secret_headers(tmp: Path):
    """Редирект на другой хост уходит без Authorization и ключа; на тот же хост — с ними.

    Стандартный `urllib` переносит все заголовки на цель редиректа: на Data Center с SSO
    токен улетал провайдеру идентификации, а подставной адрес получал ключ шлюза (BL-SEC-05)."""
    _path()
    import urllib.request
    AC = importlib.import_module("aurora_common")
    AG = importlib.import_module("agent_core")
    SC = importlib.import_module("sources_core")
    h = _Hosts()
    try:
        h.to = h.url("b", "/landing")
        # общий клиент
        req = urllib.request.Request(h.url("a", "/go"), headers={
            "Authorization": "Bearer SECRET", "Private-Token": "P", "User-Agent": "t"})
        with AC.safe_urlopen(req, timeout=10) as r:
            assert r.status == 200
        got = h.seen["b"][-1][1]
        assert "authorization" not in got and "private-token" not in got, got
        assert got.get("user-agent") == "t", "обычные заголовки должны сохраняться"
        # редирект на тот же хост — ключ остаётся
        req = urllib.request.Request(h.url("a", "/same"), headers={"Authorization": "Bearer SECRET"})
        with AC.safe_urlopen(req, timeout=10):
            pass
        assert h.seen["a"][-1][1].get("authorization") == "Bearer SECRET"
        # то же через REST-клиент Atlassian и транспорт шлюзов моделей
        h.seen["b"].clear()
        api = SC.RestApi(h.url("a"), "Bearer SECRET", str(tmp))
        assert api.get("/go") == {"ok": True}
        assert api.fetch("/go") == b'{"ok": true}'
        status, _body, err, _dt = AG.http_json(h.url("a", "/go"), None, "GW-KEY", 10)
        assert status == 200 and not err, (status, err)
        assert len(h.seen["b"]) == 3, h.seen["b"]
        assert all("authorization" not in hd for _p, hd in h.seen["b"]), h.seen["b"]
    finally:
        h.close()


@test
def test_rest_api_refuses_foreign_absolute_urls_and_cleartext(tmp: Path):
    """Абсолютный адрес от сервера (`_links.next`, вложение) с другим хостом ключ не получает;
    по открытому http (не свой компьютер) ключ не уходит, пока человек не разрешил."""
    _path()
    SC = importlib.import_module("sources_core")
    h = _Hosts()
    try:
        api = SC.RestApi(h.url("a"), "Bearer SECRET", str(tmp))
        for call in (lambda: api.get(h.url("b", "/x")), lambda: api.fetch(h.url("b", "/x")),
                     lambda: api.send("POST", h.url("b", "/x"), {})):
            try:
                call()
            except SC.UnsafeTarget:
                continue
            raise AssertionError("ключ ушёл на чужой хост")
        assert h.seen["b"] == [], "на чужой хост дошёл запрос"
        assert api.get("/same") == {"ok": True}, "свой сервер должен работать"
        # открытый http на чужой машине
        api = SC.RestApi("http://wiki.example.com", "Bearer SECRET", str(tmp))
        try:
            api.get("/rest/api/content")
            raise AssertionError("ключ ушёл по открытому http")
        except SC.UnsafeTarget as e:
            assert "AURORA_ALLOW_HTTP" in str(e)
        assert SC.RestApi("https://wiki.example.com", "Bearer S", str(tmp))._url("/a") \
            == "https://wiki.example.com/a"
        (tmp / ".env.aurora.local").write_text("AURORA_ALLOW_HTTP=1\n", encoding="utf-8")
        assert SC.RestApi("http://wiki.example.com", "Bearer S", str(tmp))._url("/a") \
            == "http://wiki.example.com/a", "явное разрешение человека должно действовать"
        # без ключа запрет не нужен: уходить нечему
        assert SC.RestApi("http://wiki.example.com", "", str(tmp))._url("/a") \
            == "http://wiki.example.com/a"
    finally:
        h.close()


@test
def test_atlassian_server_pin_rejects_a_changed_config_address(tmp: Path):
    """`AURORA_<PREFIX>_SERVER` в файле машины привязывает ключ к серверу: адрес в общем
    `aurora.config.yaml`, смененный в PR, ключ не получает — и сеть не затрагивается."""
    _path()
    CE = importlib.import_module("confluence_export")
    JE = importlib.import_module("jira_export")
    SC = importlib.import_module("sources_core")
    h = _Hosts()
    try:
        (tmp / ".env.aurora.local").write_text(f"AURORA_CONFLUENCE_SERVER={h.url('a')}\n"
                                               f"AURORA_JIRA_SERVER={h.url('a')}\n", encoding="utf-8")
        try:
            CE.Api(h.url("b"), "Bearer SECRET", str(tmp)).get("/rest/api/content")
            raise AssertionError("ключ ушёл на подменённый адрес")
        except SC.UnsafeTarget as e:
            assert "AURORA_CONFLUENCE_SERVER" in str(e)
        try:
            JE.Api(h.url("b"), "Bearer SECRET", str(tmp)).get("/rest/api/2/myself")
            raise AssertionError("ключ Jira ушёл на подменённый адрес")
        except SC.UnsafeTarget as e:
            assert "AURORA_JIRA_SERVER" in str(e)
        assert h.seen["b"] == []
        assert CE.Api(h.url("a"), "Bearer SECRET", str(tmp)).get("/ok") == {"ok": True}
        assert h.seen["a"][-1][1].get("authorization") == "Bearer SECRET"
    finally:
        h.close()


@test
def test_web_token_goes_only_to_its_own_seed_host(tmp: Path):
    """Личный токен обхода сайтов уходит на хост своей стартовой страницы, а не на любую из
    страниц списка; на открытом http (чужая машина) — тоже нет."""
    _path()
    WE = importlib.import_module("web_export")
    assert WE.token_for("https://wiki.example.com/a/b", "https://wiki.example.com/", "T") == "T"
    assert WE.token_for("https://other.example.org/a", "https://wiki.example.com/", "T") == ""
    assert WE.token_for("https://wiki.example.com:8443/a", "https://wiki.example.com/", "T") == ""
    assert WE.token_for("http://wiki.example.com/a", "http://wiki.example.com/", "T") == ""
    assert WE.token_for("http://127.0.0.1:9/a", "http://127.0.0.1:9/", "T") == "T"
    assert WE.token_for("https://wiki.example.com/a", "https://wiki.example.com/", "") == ""
    h = _Hosts()
    try:
        h.to = h.url("b", "/page")
        _raw, _enc, err = WE._get(h.url("a", "/go"), "T", 10)
        assert not err, err
        assert "authorization" not in h.seen["b"][-1][1], "токен ушёл вслед за редиректом"
    finally:
        h.close()


@test
def test_models_list_does_not_send_a_saved_key_to_another_address(tmp: Path):
    """`/api/models/list`: сохранённый ключ провайдера уходит только на его же адрес;
    на другой — отказ без запроса; введённый в форме ключ идёт как есть."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    AG = importlib.import_module("agent_core")
    MC = importlib.import_module("model_config")
    calls = []
    real_models_of, real_data = AG.models_of, ck.models_data
    AG.models_of = lambda b, timeout=20: calls.append(dict(b)) or {"n": 0, "url": b["url"],
                                                                    "models": ["m1"]}
    ck.models_data = lambda: {"providers": [{"id": "p1", "url": "https://gw.example.com/v1",
                                             "key": "SAVED-KEY"}]}
    try:
        ok = ck.models_list({"id": "p1", "url": "https://GW.example.com/v1/", "key": MC.MASK})
        assert ok.get("models") == ["m1"] and calls[-1]["key"] == "SAVED-KEY", (ok, calls)
        n = len(calls)
        for evil in ("https://evil.example.com/v1", "http://gw.example.com/v1",
                     "https://gw.example.com:8443/v1"):
            for key in (MC.MASK, ""):
                r = ck.models_list({"id": "p1", "url": evil, "key": key})
                assert r.get("error") and "models" not in r, (evil, r)
        assert len(calls) == n, "запрос на чужой адрес ушёл"
        typed = ck.models_list({"id": "p1", "url": "https://evil.example.com/v1", "key": "TYPED"})
        assert typed.get("models") and calls[-1]["key"] == "TYPED", "введённый ключ — выбор человека"
        # провайдер без сохранённого ключа (локальный шлюз): проверять любой адрес можно
        ck.models_data = lambda: {"providers": [{"id": "p2", "url": "http://127.0.0.1:8080/v1",
                                                 "key": ""}]}
        assert ck.models_list({"id": "p2", "url": "http://127.0.0.1:9090/v1", "key": ""}).get("models")
    finally:
        AG.models_of, ck.models_data = real_models_of, real_data


@test
def test_atlassian_server_is_remembered_and_a_changed_address_is_refused(tmp: Path):
    """Без привязки в файле машины первый удачный ответ запоминает сервер (вне git), и другой
    `base_url` в общем конфиге ключа уже не получает; строка `AURORA_<PREFIX>_SERVER` — принятие
    нового адреса. Свой компьютер не запоминается, проект без `.aurora/` — тоже."""
    _path()
    SC = importlib.import_module("sources_core")
    JE = importlib.import_module("jira_export")
    (tmp / ".aurora").mkdir()
    root = str(tmp)
    # проект без запомненного сервера: любой https-адрес пока допустим
    assert SC.RestApi("https://jira.example.com", "Bearer S", root)._url("/a") \
        == "https://jira.example.com/a"
    SC.remember_origin("JIRA", "https://jira.example.com", root)
    assert SC.trusted_origin("JIRA", root) == "https://jira.example.com"
    SC.remember_origin("JIRA", "https://other.example.org", root)       # первое запоминание не меняется
    assert SC.trusted_origin("JIRA", root) == "https://jira.example.com"
    state = json.loads((tmp / ".aurora" / "state" / "trusted-hosts.json").read_text(encoding="utf-8"))
    assert state == {"JIRA": "https://jira.example.com"}, state
    # подмена адреса — отказ с причиной и подсказкой, как принять
    try:
        JE.Api("https://jira-evil.example.com", "Bearer S", root)._url("/rest/api/2/myself")
        raise AssertionError("ключ ушёл на подменённый адрес")
    except SC.UnsafeTarget as e:
        assert "AURORA_JIRA_SERVER=https://jira-evil.example.com" in str(e), str(e)
    # тот же сервер и обновление http → https проходят; другой префикс (Confluence) не затронут
    assert JE.Api("https://jira.example.com/", "Bearer S", root)._url("/a") == "https://jira.example.com/a"
    assert SC.RestApi("https://x.example.com", "Bearer S", root)._url("/a") == "https://x.example.com/a"
    # без ключа уходить нечему
    assert JE.Api("https://jira-evil.example.com", "", root)._url("/a") == "https://jira-evil.example.com/a"
    # человек принял переезд строкой в файле машины
    (tmp / ".env.aurora.local").write_text("AURORA_JIRA_SERVER=https://jira-evil.example.com\n",
                                           encoding="utf-8")
    assert JE.Api("https://jira-evil.example.com", "Bearer S", root)._url("/a") \
        == "https://jira-evil.example.com/a"
    # свой компьютер и проект без .aurora/ не запоминаются
    other = tmp / "other"
    other.mkdir()
    SC.remember_origin("JIRA", "http://127.0.0.1:9", str(other))
    SC.remember_origin("JIRA", "https://jira.example.com", str(other))
    assert not (other / ".aurora").exists()
    (other / ".aurora").mkdir()
    SC.remember_origin("JIRA", "http://127.0.0.1:9", str(other))
    assert SC.trusted_origin("JIRA", str(other)) == ""
    # настоящий ответ сервера запоминает его (свой компьютер для проверки выдан за чужой)
    h = _Hosts()
    real = SC.is_loopback_host
    try:
        third = tmp / "third"
        (third / ".aurora").mkdir(parents=True)
        (third / ".env.aurora.local").write_text("AURORA_ALLOW_HTTP=1\n", encoding="utf-8")
        SC.is_loopback_host = lambda host: False
        api = JE.Api(h.url("a"), "Bearer S", str(third))
        assert api.get("/ok") == {"ok": True}
        assert SC.trusted_origin("JIRA", str(third)) == h.url("a")
        try:
            JE.Api(h.url("b"), "Bearer S", str(third)).get("/ok")
            raise AssertionError("ключ ушёл на другой сервер")
        except SC.UnsafeTarget as e:
            assert "изменился" in str(e), str(e)
        assert h.seen["b"] == []
    finally:
        SC.is_loopback_host = real
        h.close()


@test
def test_watchdog_probe_sends_no_key_to_a_foreign_host_or_cleartext(tmp: Path):
    """Сторож зависаний проверяет связь с Jira/Confluence тем же клиентом: редирект на другой
    хост уходит без Authorization, а открытый http (чужая машина) не опрашивается вовсе."""
    _path()
    WD = importlib.import_module("watchdog")
    AG = importlib.import_module("agent_core")
    h = _Hosts()
    MC = importlib.import_module("model_config")
    real = (AG.models_of, AG.kit_env, MC.load)
    # Модели и настройка кита сторожу в тесте не нужны: к настоящим шлюзам и файлам кита не ходим.
    AG.models_of, AG.kit_env = (lambda b, timeout=20: {}), (lambda *a, **k: {})
    MC.load = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("тест"))
    try:
        h.to = h.url("b", "/landing")
        (tmp / "aurora.config.yaml").write_text(
            f'atlassian:\n  jira:\n    base_url: "{h.url("a")}"\n    project_key: "P"\n',
            encoding="utf-8")
        (tmp / ".env.aurora.local").write_text("JIRA_PAT=FAKE-JIRA-TOKEN\n", encoding="utf-8")
        import atlassian_ops as AO
        # живой сервер: связь есть, ключ ушёл только на свой адрес
        got = WD.probe(str(tmp))
        assert got == {"ok": True, "down": []}, got
        assert h.seen["a"][-1][1].get("authorization") == "Bearer FAKE-JIRA-TOKEN"
        # сервер отвечает редиректом на чужой хост: туда ключ не доходит
        h.redirect_all = True
        h.seen["a"].clear()
        got = WD.probe(str(tmp))
        assert got == {"ok": True, "down": []}, got
        assert h.seen["a"][-1][1].get("authorization") == "Bearer FAKE-JIRA-TOKEN"
        assert h.seen["b"] and all("authorization" not in hd for _p, hd in h.seen["b"]), h.seen["b"]
        # открытый http на чужой машине: запроса нет, это не «связь упала»
        (tmp / "aurora.config.yaml").write_text(
            'atlassian:\n  jira:\n    base_url: "http://jira.example.com"\n    project_key: "P"\n',
            encoding="utf-8")
        assert AO.settings(str(tmp))["jira_url"] == "http://jira.example.com"
        assert WD.probe(str(tmp)) == {"ok": True, "down": []}
    finally:
        AG.models_of, AG.kit_env, MC.load = real
        h.close()


@test
def test_report_scripts_send_the_key_only_to_their_own_server(tmp: Path):
    """Шаги отчёта аналитика ходят в Jira/Confluence через `paths.open_secure`: редирект на
    другой хост уходит без Authorization, чужой/открытый адрес отклоняется до запроса."""
    _path()
    rp = str(KIT / "reports" / "analyst")
    if rp not in sys.path:
        sys.path.insert(0, rp)
    cwd = os.getcwd()
    os.chdir(tmp)           # отчёт читает привязку машины из папки проекта
    try:
        paths = importlib.import_module("paths")
        SC = importlib.import_module("sources_core")
        import urllib.request
        h = _Hosts()
        try:
            h.to = h.url("b", "/landing")
            req = urllib.request.Request(h.url("a", "/go"), headers={"Authorization": "Bearer FAKE"})
            with paths.open_secure(req, h.url("a"), "JIRA"):
                pass
            assert h.seen["a"][-1][1].get("authorization") == "Bearer FAKE"
            assert h.seen["b"] and all("authorization" not in hd for _p, hd in h.seen["b"]), h.seen["b"]
            n = len(h.seen["b"])
            for evil in (h.url("b", "/x"), "http://jira.example.com/rest"):
                try:
                    paths.open_secure(urllib.request.Request(evil, headers={"Authorization": "Bearer FAKE"}),
                                      h.url("a"), "JIRA")
                    raise AssertionError("ключ ушёл на чужой адрес")
                except SC.UnsafeTarget:
                    pass
            assert len(h.seen["b"]) == n
        finally:
            h.close()
        # привязка машины: base_url, не совпавший с ней, ключа не получает
        (tmp / ".env.aurora.local").write_text("AURORA_JIRA_SERVER=https://jira.example.com\n",
                                               encoding="utf-8")
        try:
            paths.open_secure(urllib.request.Request("https://jira-evil.example.com/x"),
                              "https://jira-evil.example.com", "JIRA")
            raise AssertionError("ключ ушёл на подменённый адрес")
        except SC.UnsafeTarget as e:
            assert "AURORA_JIRA_SERVER" in str(e)
    finally:
        os.chdir(cwd)


@test
def test_a_refused_target_is_one_line_not_a_traceback(tmp: Path):
    """Отказ `UnsafeTarget` в командных скриптах — одна строка причины и код 1, без трейсбека."""
    proj = tmp / "p"
    proj.mkdir()
    (proj / "aurora.config.yaml").write_text(
        'atlassian:\n  jira:\n    base_url: "http://jira.example.com"\n    project_key: "P"\n'
        '  confluence:\n    base_url: "http://wiki.example.com"\n    space: "S"\n    sync_roots:\n'
        '      - page_id: "1"\n      - page_id: "2"\n', encoding="utf-8")
    (proj / ".env.aurora.local").write_text("JIRA_PAT=FAKE\nCONFLUENCE_PAT=FAKE\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "AURORA_ALLOW_HTTP"}
    for script in ("jira_export.py", "confluence_export.py"):
        cp = subprocess.run([sys.executable, str(SCRIPTS / script)] + (["--limit", "1"] if script[0] == "j" else []),
                            cwd=str(proj), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env)
        assert cp.returncode == 1, (script, cp.returncode, cp.stderr[-400:])
        assert "Traceback" not in cp.stderr and "AURORA_ALLOW_HTTP" in cp.stderr, cp.stderr[-400:]
        assert cp.stderr.count("AURORA_ALLOW_HTTP") == 1, (script, cp.stderr)


@test
def test_web_token_is_pinned_to_the_hosts_the_machine_lists(tmp: Path):
    """`AURORA_WEB_TOKEN_HOSTS` в файле машины: токен только перечисленным хостам; страница,
    добавленная в общий список, его не получает, и человек видит причину."""
    _path()
    WE = importlib.import_module("web_export")
    hosts = WE.token_hosts({"AURORA_WEB_TOKEN_HOSTS": "Wiki.example.com, https://docs.example.com:8443/x;"})
    assert hosts == ["wiki.example.com", "docs.example.com"], hosts
    assert WE.token_hosts({}) == []
    assert WE.token_for("https://wiki.example.com/a", "https://wiki.example.com/", "T", hosts) == "T"
    assert WE.token_for("https://new.example.org/a", "https://new.example.org/", "T", hosts) == ""
    assert "AURORA_WEB_TOKEN_HOSTS" in WE.token_refusal(
        "https://new.example.org/a", "https://new.example.org/", "T", hosts)
    # привязки нет — прежнее правило: токен хосту своей стартовой страницы
    assert WE.token_for("https://new.example.org/a", "https://new.example.org/", "T", []) == "T"
    assert WE.token_refusal("https://x.example.org/", "https://x.example.org/", "", hosts) == ""


@test
def test_saved_secrets_do_not_follow_a_changed_server_on_save(tmp: Path):
    """Сохранение настройки с маской вместо ключа и другим адресом сервера не переносит
    старый ключ на новый адрес (Git: замечание `required`; провайдер моделей: ключ пуст)."""
    _path()
    GS = importlib.import_module("git_sync")
    MC = importlib.import_module("model_config")
    # провайдеры моделей
    old = {"providers": [{"id": "p1", "url": "https://gw.example.com/v1", "key": "SAVED"},
                         {"id": "p2", "url": "gateway.local:8080", "key": "K2"}]}
    new = {"providers": [{"id": "p1", "url": "https://GW.example.com/v1/", "key": MC.MASK},
                         {"id": "p2", "url": "gateway.local:8080", "key": MC.MASK}]}
    got = MC.unmask(new, old)["providers"]
    assert [p["key"] for p in got] == ["SAVED", "K2"], got
    new["providers"][0]["url"] = "https://evil.example.com/v1"
    assert MC.unmask(new, old)["providers"][0]["key"] == "", "ключ ушёл вслед за подменой адреса"
    # Git: учётные данные хранятся в домашней папке — подменяем на временную
    real = GS.cred_file
    GS.cred_file = lambda: tmp / "home" / "credentials.json"
    proj = tmp / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    try:
        s1 = {"provider": "gitea", "instance": "https://git.example.com", "repo": "team/repo",
              "remote": "origin", "branch": "main", "auth": "token"}
        GS.cred_save(str(proj), {**GS.CRED_DEFAULT, "auth": "token", "user": "u", "secret": "SAVED-TOKEN"})
        assert GS.save(str(proj), s1, {"auth": "token", "user": "u", "secret": GS.MASK}).get("ok"), \
            "тот же сервер: ключ остаётся"
        assert GS.cred_load(str(proj))["secret"] == "SAVED-TOKEN"
        s2 = {**s1, "instance": "https://git-evil.example.com"}
        r = GS.save(str(proj), s2, {"auth": "token", "user": "u", "secret": GS.MASK})
        assert not r.get("ok") and {"field": "secret", "code": "required"} in r["problems"], r
        assert GS.cred_load(str(proj))["secret"] == "SAVED-TOKEN", "прежнее сохранённое не тронуто"
        r = GS.save(str(proj), s2, {"auth": "token", "user": "u", "secret": "NEW-TOKEN"})
        assert r.get("ok") and GS.cred_load(str(proj))["secret"] == "NEW-TOKEN", r
        # человек вписал тот же ключ на новом адресе (переезд сервера): вписанное побеждает
        r = GS.save(str(proj), {**s1, "instance": "https://git2.example.com"},
                    {"auth": "token", "user": "u", "secret": "NEW-TOKEN"})
        assert r.get("ok") and GS.cred_load(str(proj))["secret"] == "NEW-TOKEN", r
    finally:
        GS.cred_file = real


@test
def test_web_token_follows_the_hosts_where_it_first_worked(tmp: Path):
    """Без строки `AURORA_WEB_TOKEN_HOSTS` токен обхода сайтов запоминает хосты первого
    удачного прогона (`.aurora/state/trusted-hosts.json`): страница, добавленная в общий
    конфиг позже на другом хосте, его не получает, а причина называет, что сделать."""
    _path()
    WE = importlib.import_module("web_export")
    SC = importlib.import_module("sources_core")
    proj = tmp / "p"
    (proj / ".aurora").mkdir(parents=True)
    sent = []
    real_get, real_argv, cwd = WE._get, sys.argv, os.getcwd()
    WE._get = lambda url, token="", timeout=0, headers=None, meta=None: (
        sent.append((url, token)) or (b"<html><body>x</body></html>", "utf-8", ""))

    def go(pages: list, env_lines: str = "") -> str:
        (proj / "aurora.config.yaml").write_text(
            "web:\n  pages:\n" + "".join(f"    - url: {p}\n      trusted: false\n" for p in pages),
            encoding="utf-8")
        (proj / ENV_LOCAL).write_text("WEB_PERSONAL_TOKEN=SECRET\n" + env_lines, encoding="utf-8")
        sent.clear()
        sys.argv = ["web_export.py", "--out", str(proj / "out")]
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            WE.main()
        return buf.getvalue()

    os.chdir(proj)
    try:
        go(["https://wiki.example.com/a", "https://docs.example.com/b"])
        assert all(t == "SECRET" for _u, t in sent) and sent, sent        # первый прогон: оба хоста
        assert SC.trusted_hosts("WEB") == ["docs.example.com", "wiki.example.com"]
        # страницу добавили в общий список позже: токен ей не уходит, причина видна
        out = go(["https://wiki.example.com/a", "https://new.example.org/c"])
        got = dict(sent)
        assert got["https://wiki.example.com/a"] == "SECRET" and got["https://new.example.org/c"] == "", sent
        assert "чтобы разрешить новый адрес" in out and "AURORA_WEB_TOKEN_HOSTS" in out, out
        # подсказка готовая: все прежние хосты и новый (строка заменяет запомненный список)
        assert ("AURORA_WEB_TOKEN_HOSTS=docs.example.com,wiki.example.com,new.example.org" in out), out
        # строка машины главнее запомненного: хост, которого нет в ней, токена не получает
        go(["https://wiki.example.com/a", "https://docs.example.com/b"],
           "AURORA_WEB_TOKEN_HOSTS=docs.example.com\n")
        assert dict(sent) == {"https://wiki.example.com/a": "", "https://docs.example.com/b": "SECRET"}, sent
        # проект без `.aurora/` ничего не запоминает (прежнее правило: хост своей страницы)
        bare = tmp / "bare"
        bare.mkdir()
        SC.remember_hosts("WEB", ["x.example.com"], str(bare))
        assert not (bare / ".aurora").exists() and SC.trusted_hosts("WEB", str(bare)) == []
    finally:
        os.chdir(cwd)
        WE._get, sys.argv = real_get, real_argv


@test
def test_web_token_hosts_are_remembered_only_when_every_host_answered(tmp: Path):
    """Запоминание хостов токена не закрепляет случайный набор: сбой сети у одного из хостов
    первого прогона оставляет запоминание на следующий, а свой компьютер в список не попадает."""
    _path()
    WE = importlib.import_module("web_export")
    SC = importlib.import_module("sources_core")
    proj = tmp / "p"
    (proj / ".aurora").mkdir(parents=True)
    down: set = set()
    sent = []

    def fake(url, token="", timeout=0, headers=None, meta=None):
        sent.append((url, token))
        if any(h in url for h in down):
            return b"", "", "URLError: сеть недоступна (попыток: 3)"
        if "denied" in url:
            return b"", "", "HTTP 403"
        return b"<html><body>x</body></html>", "utf-8", ""

    real_get, real_argv, cwd = WE._get, sys.argv, os.getcwd()
    WE._get = fake

    def go(pages: list) -> None:
        (proj / "aurora.config.yaml").write_text(
            "web:\n  pages:\n" + "".join(f"    - url: {p}\n      trusted: false\n" for p in pages),
            encoding="utf-8")
        (proj / ENV_LOCAL).write_text("WEB_PERSONAL_TOKEN=SECRET\nAURORA_ALLOW_HTTP=1\n",
                                      encoding="utf-8")
        sent.clear()
        sys.argv = ["web_export.py", "--out", str(proj / "out")]
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            WE.main()

    os.chdir(proj)
    try:
        pages = ["https://wiki.example.com/a", "https://docs.example.org/b"]
        down.add("wiki.example.com")
        go(pages)                                   # VPN выключен: ответил только один хост
        assert SC.trusted_hosts("WEB") == [], SC.trusted_hosts("WEB")
        down.clear()
        go(pages)                                   # сеть вернулась: запомнены оба
        assert SC.trusted_hosts("WEB") == ["docs.example.org", "wiki.example.com"], SC.trusted_hosts("WEB")
        # свой компьютер не запоминается (как и в remember_origin), а HTTP-отказ — это ответ
        (proj / ".aurora" / "state" / "trusted-hosts.json").unlink()
        go(["http://127.0.0.1:9/a", "https://denied.example.com/x"])
        assert SC.trusted_hosts("WEB") == ["denied.example.com"], SC.trusted_hosts("WEB")
        (proj / ".aurora" / "state" / "trusted-hosts.json").unlink()
        go(["http://127.0.0.1:9/a"])
        assert SC.trusted_hosts("WEB") == [], SC.trusted_hosts("WEB")
        go(["https://wiki.example.com/a"])          # токен не привязан к петле
        assert SC.trusted_hosts("WEB") == ["wiki.example.com"] and dict(sent)["https://wiki.example.com/a"] == "SECRET"
    finally:
        os.chdir(cwd)
        WE._get, sys.argv = real_get, real_argv


@test
def test_web_export_reads_the_machine_file_when_run_directly(tmp: Path):
    """Прямой запуск `web_export.py` (не из панели): `AURORA_WEB_TOKEN_HOSTS` и `AURORA_ALLOW_HTTP`
    берутся из файла машины, а не только из окружения процесса. Раньше строка из файла молча
    не действовала: страница вне привязки получала токен, а http из файла не разрешался."""
    _path()
    WE = importlib.import_module("web_export")
    proj = tmp / "p"
    proj.mkdir()
    sent = []
    real_get, real_argv, cwd = WE._get, sys.argv, os.getcwd()
    WE._get = lambda url, token="", timeout=0, headers=None, meta=None: (
        sent.append((url, token)) or (b"", "", "HTTP 404"))

    def go(page: str, env_lines: str) -> str:
        (proj / "aurora.config.yaml").write_text(
            f"web:\n  pages:\n    - url: {page}\n      trusted: false\n", encoding="utf-8")
        (proj / ENV_LOCAL).write_text("WEB_PERSONAL_TOKEN=SECRET\n" + env_lines, encoding="utf-8")
        sent.clear()
        sys.argv = ["web_export.py", "--out", str(proj / "out")]
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            WE.main()
        return buf.getvalue()

    os.chdir(proj)
    try:
        # привязка из файла: страница на другом хосте токен не получает, причина видна
        out = go("https://new.example.org/page", "AURORA_WEB_TOKEN_HOSTS=wiki.example.com\n")
        assert sent and all(t == "" for _u, t in sent), sent
        assert "AURORA_WEB_TOKEN_HOSTS" in out, out
        # контроль: без привязки токен уходит хосту своей страницы (значит, он читался из файла)
        go("https://new.example.org/page", "")
        assert sent and sent[0][1] == "SECRET", sent
        # открытый http чужой машины: без разрешения токен не уходит, с разрешением из файла — уходит
        out = go("http://wiki.example.org/page", "")
        assert sent[0][1] == "" and "AURORA_ALLOW_HTTP" in out, (sent, out)
        out = go("http://wiki.example.org/page", "AURORA_ALLOW_HTTP=1\n")
        assert sent[0][1] == "SECRET" and "токен не отправляется" not in out, (sent, out)
    finally:
        os.chdir(cwd)
        WE._get, sys.argv = real_get, real_argv


@test
def test_models_save_tells_which_key_was_dropped_on_an_address_change(tmp: Path):
    """Смена адреса провайдера при ключе-маске: ключ не переносится, и ответ сохранения
    называет провайдера (на языке интерфейса), а не молча даёт потом 401."""
    _path()
    ck = importlib.import_module("aurora_cockpit")
    MC = importlib.import_module("model_config")
    old = {"providers": [{"id": "p1", "url": "https://gw.example.com/v1", "key": "SAVED"}]}
    dropped: list = []
    MC.unmask({"providers": [{"id": "p1", "url": "https://gw.example.com/v1/", "key": MC.MASK}]},
              old, dropped)
    assert dropped == [], "тот же сервер — ключ остаётся, замечания нет"
    MC.unmask({"providers": [{"id": "p1", "url": "https://evil.example.com/v1", "key": MC.MASK}]},
              old, dropped)
    assert dropped == ["p1"], dropped
    real_data, real_save = ck.models_data, MC.save
    ck.models_data = lambda: old
    MC.save = lambda kit, data: {"ok": True, "notes": [], "path": ""}
    try:
        r = ck.models_save({"models": {"providers": [
            {"id": "p1", "url": "https://evil.example.com/v1", "key": MC.MASK}]}})
        assert r["ok"] and r["notes"] == [ck.models_key_note("p1")], r
        en = ck.translate_payload(r, "en")
        assert "p1" in en["notes"][0] and "enter its key again" in en["notes"][0], en
    finally:
        ck.models_data, MC.save = real_data, real_save


@test
def test_review_run_says_why_the_server_is_refused(tmp: Path):
    """`review_run.py`: сервер, на который ключ отправлять нельзя (открытый http чужой машины),
    даёт одну строку причины и код 1, а не «страница не прочитана» без объяснения."""
    proj = tmp / "p"
    proj.mkdir()
    (proj / "aurora.config.yaml").write_text(
        'atlassian:\n  confluence:\n    base_url: "http://wiki.example.com"\n    space: "S"\n',
        encoding="utf-8")
    (proj / ENV_LOCAL).write_text("CONFLUENCE_PAT=FAKE\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "AURORA_ALLOW_HTTP"}
    cp = subprocess.run([sys.executable, str(SCRIPTS / "review_run.py"), "--page", "1"],
                        cwd=str(proj), capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env)
    assert cp.returncode == 1, (cp.returncode, cp.stdout[-300:], cp.stderr[-400:])
    assert "Traceback" not in cp.stderr and "AURORA_ALLOW_HTTP" in cp.stderr, cp.stderr[-400:]
