"""Проверки движка Aurora, часть 38: чужой текст в панели не становится кодом в её origin.

Каркас и помощники — tests/harness.py. Зеркала Confluence и сайтов пишут посторонние, имена
людей в отчётах — пользователи чужой Jira. У origin панели токен сессии, а токен даёт запуск
команд, поэтому mermaid в редакторе строгий, у страницы есть CSP, а отчёт отдаётся в песочнице.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import re
import sys

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
        c.request("GET", path)
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
        base = f"/api/report/file?project={tmp}&id=analyst&t={ck.TOKEN}"
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
