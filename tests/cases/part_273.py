"""Проверки W4 R5: рамка «Выпуск в Word», статусы эталона и скрипта-плана.

Каркас и помощники — tests/harness.py. Проверки не запускают модель и не ходят в сеть:
эталон собирается W1 из синтетического примера, скрипт — декларативный план W5 из
markdown-документа во временном проекте. Отдельно проверяется, что `/api/docx/*`
доступен только локальной сессии и что пути не выходят за `real_rel` проекта.
"""
from __future__ import annotations

import http.client
import importlib
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, _js_function, make_project, panel_sources, set_home, test

for _p in (str(SCRIPTS), str(KIT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from docfix import reference_base, reference_docx, replace_part  # noqa: E402


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _ck():
    _mod("aurora_common")
    if str(KIT / "cockpit") not in sys.path:
        sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


def _project(tmp: Path) -> Path:
    """Проект с полным набором входов Word, но без собранных эталона и плана."""
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Prompts").mkdir(exist_ok=True)
    (root / "Artifacts" / "opz").mkdir(parents=True, exist_ok=True)
    reference_docx(root / "Templates" / "example.docx")
    reference_base(root / "Templates" / "base.docx")
    (root / "Templates" / "title.xml").write_text(
        '<title><line>Описание {project}</line></title>\n', encoding="utf-8")
    (root / "Prompts" / "opz_word.md").write_text(
        "# Word\n\nСохранить структуру разделов.\n", encoding="utf-8")
    (root / "Prompts" / "opz_checks.json").write_text(json.dumps({
        "version": 1, "kind": "opz",
        "required_sections": [{"level": 1, "text": "Введение"}],
        "title_fields": {"project": "Test"},
    }, ensure_ascii=False), encoding="utf-8")
    (root / "Artifacts" / "opz" / "doc.md").write_text(
        "# Введение\n\n## Состав\n\nТекст документа.\n", encoding="utf-8")
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Test"\n  slug: "Test"\n'
        'artifacts:\n  opz:\n    title: "ОПЗ"\n    template: Templates/example.docx\n'
        '    out: Artifacts/opz\n    docx_example: Templates/example.docx\n'
        '    docx_template: Templates/base.docx\n    docx_title: Templates/title.xml\n'
        '    docx_prompt: Prompts/opz_word.md\n    docx_checks: Prompts/opz_checks.json\n'
        '    docx_engine: auto\n    docx_map: \'{"Heading1":"Heading1"}\'\n',
        encoding="utf-8")
    return root


class _Panel:
    """Настоящий HTTP-сервер панели на временном корне проектов."""

    def __init__(self, tmp: Path):
        self.restore = set_home(tmp / "home")
        self.ck = _ck()
        self.roots = tmp / "roots"
        self.roots.mkdir()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.ck.Handler)
        self.srv.roots = [str(self.roots)]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=90)
        headers = {"X-Aurora-Token": self.ck.TOKEN}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8") or "{}")

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.restore()


@test
def test_word_statuses_roundtrip_and_stale_inputs(tmp: Path):
    """`kinds_read` отдаёт Word-статусы, API собирает эталон и план, а изменившийся
    вход переводит нужный статус в `stale` с кодом причины."""
    import time
    panel = _Panel(tmp)
    try:
        root = _project(panel.roots)
        project = str(root)
        st, got = panel.call("GET", "/api/kinds?project=" + project)
        assert st == 200 and got["docx"]["opz"]["reference"]["state"] == "missing", (st, got)
        assert got["docx"]["opz"]["script"]["state"] == "missing", got["docx"]
        assert "docx_reference" not in got["kinds"]["opz"], "служебный статус попал в реестр видов"

        st, got = panel.call("GET", "/api/docx/reference?project=" + project + "&kind=opz")
        assert st == 200 and got["reference"]["state"] == "missing" and not got["reference"]["path"].startswith("/"), got
        st, got = panel.call("POST", "/api/docx/reference/create", {"project": project, "kind": "opz"})
        assert st == 200 and got["ok"] and got["reference"]["state"] == "ready", (st, got)
        reference = root / "Scripts" / "Generated" / "opz.docx"
        assert reference.is_file() and got["reference"]["path"] == "Scripts/Generated/opz.docx", got

        st, got = panel.call("GET", "/api/docx/reference/status?project=" + project + "&kind=opz")
        assert st == 200 and got["reference"]["state"] == "ready", (st, got)
        st, got = panel.call("POST", "/api/docx/script", {"project": project, "kind": "opz"})
        assert st == 200 and got["ok"] and got["script"]["state"] == "ready", (st, got)
        plan = root / "Scripts" / "Generated" / "opz.docx.json"
        assert plan.is_file() and got["script"]["path"] == "Scripts/Generated/opz.docx.json", got
        data = json.loads(plan.read_text(encoding="utf-8"))
        assert data["kind"] == "opz" and data["sections"][0]["heading"] == "Введение", data
        assert not list((root / "Scripts" / "Generated").glob("*.py")), "W4 собрал исполняемый скрипт"

        # Изменение источника плана — stale именно у скрипта, эталон не трогается.
        time.sleep(0.01)
        (root / "Artifacts" / "opz" / "doc.md").write_text(
            "# Введение\n\n## Состав\n\nНовый текст.\n", encoding="utf-8")
        st, got = panel.call("GET", "/api/docx/script/status?project=" + project + "&kind=opz")
        assert st == 200 and got["script"]["state"] == "stale" and "source-changed" in got["script"]["why_codes"], got
        st, got = panel.call("GET", "/api/docx/reference?project=" + project + "&kind=opz")
        assert got["reference"]["state"] == "ready", got

        # Изменение примера — stale у эталона.
        time.sleep(0.01)
        replace_part(str(root / "Templates" / "example.docx"), "word/document.xml",
                     "Первый заголовок", "Другой заголовок")
        st, got = panel.call("GET", "/api/kinds?project=" + project)
        assert got["docx"]["opz"]["reference"]["state"] == "stale", got["docx"]
        assert "example-changed" in got["docx"]["opz"]["reference"]["why_codes"], got["docx"]
    finally:
        panel.close()


@test
def test_word_api_is_local_only_and_paths_stay_inside(tmp: Path):
    """`/api/docx/*` не входит в remote allowlist; `..`, абсолютный путь и ссылка наружу
    дают stale/отказ, а не чтение чужого файла."""
    import aurora_guard as G
    remote = G.AuthContext(kind="remote", token_id="t", user="u")
    for method, path in (("GET", "/api/docx/reference"), ("GET", "/api/docx/reference/status"),
                         ("GET", "/api/docx/script"), ("GET", "/api/docx/script/status"),
                         ("POST", "/api/docx/reference/create"), ("POST", "/api/docx/reference"),
                         ("POST", "/api/docx/script/create"), ("POST", "/api/docx/script")):
        assert G.remote_allowed(remote, method, path) is False, (method, path)

    panel = _Panel(tmp)
    try:
        root = _project(panel.roots)
        project = str(root)
        outside = tmp / "outside.docx"
        reference_docx(outside)
        cfg = root / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "docx_example: Templates/example.docx", "docx_example: ../outside.docx"), encoding="utf-8")
        st, got = panel.call("GET", "/api/docx/reference?project=" + project + "&kind=opz")
        assert st == 200 and got["reference"]["state"] == "stale", got
        assert "outside" in got["reference"]["why_codes"], got
        st, got = panel.call("POST", "/api/docx/reference/create?lang=en", {"project": project, "kind": "opz"})
        assert st == 400 and "outside the project" in got.get("error", ""), (st, got)
        # Ошибка самого W1 приходит стабильной строкой, которую страница переводит;
        # техническая причина остаётся в detail и в подпись статуса не попадает.
        (root / "Templates" / "broken.docx").write_bytes(b"not a zip")
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "docx_example: ../outside.docx", "docx_example: Templates/broken.docx"), encoding="utf-8")
        st, got = panel.call("POST", "/api/docx/reference/create?lang=en", {"project": project, "kind": "opz"})
        assert st == 400 and got.get("error") == "the Word reference could not be built", (st, got)
        assert got.get("detail"), "техническая причина W1 потерялась"

        # `kind` не становится сегментом пути, даже если он пришёл в запросе.
        st, got = panel.call("POST", "/api/docx/script/create", {"project": project, "kind": "../escaped"})
        assert st == 404 and not (root / "Scripts" / "escaped.docx.json").exists(), (st, got)
        assert not (tmp / "escaped.docx.json").exists()
    finally:
        panel.close()


@test
def test_word_panel_frame_fields_filters_and_i18n(tmp: Path):
    """Четвёртая рамка, поля, фильтры выбора и кнопки — в панели; строки есть в ru и en."""
    ui = panel_sources()
    render = _js_function(ui, "async function renderKinds(")
    assert '"data-frame":"docx"' in render, "в форме видов нет четвёртой рамки Word"
    for key in ("docx_template", "docx_example", "docx_title", "docx_logo",
                "docx_prompt", "docx_checks", "docx_engine", "docx_map"):
        assert f'lab("{key}"' in render, f"в рамке Word нет поля {key}"
    assert "extPicker([\".docx\",\".dotx\"])" in render
    assert "extPicker([\".docx\",\".xml\"])" in render
    assert "/api/docx/reference/create" in render and "/api/docx/script/create" in render
    assert "/api/docx/${which}/create" not in render, "динамический путь не сверяется с сервером"
    assert "Собрать" not in render  # подписи кнопок живут в i18n, не в коде
    for lang in ("ru", "en"):
        cat = json.loads((KIT / "cockpit" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert cat["kinds.frame_docx"] and cat["kinds.docx_reference_create"] and cat["kinds.docx_script_create"], lang
        assert cat["kinds.docx_engine_auto"] and cat["kinds.docx_engine_plan"], lang
    ru = json.loads((KIT / "cockpit" / "i18n" / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit" / "i18n" / "en.json").read_text(encoding="utf-8"))
    keys = {k for k in ru if k.startswith(("kinds.docx", "kinds.l_docx", "kinds.f_docx", "kinds.tip_docx"))}
    assert keys and keys <= set(en), sorted(keys - set(en))
    assert ru["kinds.docx_reference_create"] == "Собрать эталон"
    assert ru["kinds.docx_script_create"] == "Собрать скрипт"


@test
def test_word_write_sites_are_declared_in_the_ratchet(tmp: Path):
    """Новые writer'ы Word названы в `tests/card_write_sites.json`: это не карточки,
    и число вызовов записи в каждом месте совпадает с кодом."""
    W = importlib.import_module("card_write_sites")
    data = W.load()
    expected = {
        "cockpit/aurora_cockpit.py:word_script_create": (2, "none"),
        "scripts/docx_plan.py:_write_json": (1, "none"),
        "scripts/docx_reference.py:build": (3, "none"),
        "scripts/docx_title.py:save": (1, "shared"),
    }
    for key, (calls, git) in expected.items():
        rec = (data.get("sites") or {}).get(key) or {}
        assert rec.get("kind") == "other", (key, rec)
        assert rec.get("calls") == calls and rec.get("git") == git, (key, rec)
        assert rec.get("reason"), (key, rec)
    problems = W.verdict(W.scan(W.KIT), data, W.scan_lines(W.KIT))
    assert not problems, "\n".join(problems)


@test
def test_word_statuses_stay_local_and_panel_renders_them_as_text(tmp: Path):
    """Word-статусы не уходят удалённому `/api/kinds`, старый конфиг не получает
    docx-полей от чтения, а подписи статусов в панели рисуются текстом, не HTML."""
    import aurora_guard as G
    ck = _ck()
    remote = G.AuthContext(kind="remote", token_id="t", user="u")
    session = G.AuthContext(kind="session", token_id="s", user="u")
    data = {"kinds": {"ac": {}}, "docx": {"ac": {"reference": {"state": "ready"}}}}
    assert "docx" not in ck.kinds_for_client(data, remote)
    assert "docx" in ck.kinds_for_client(data, session)
    assert "docx" in ck.kinds_for_client(data, None)

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "AC.md").write_text("# AC\n", encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text('project:\n  name: "Old"\nartifacts:\n  ac:\n    title: "AC"\n'
                   '    template: Templates/AC.md\n    out: Artifacts/ac\n', encoding="utf-8")
    before = _mod("make_kinds").read_kinds(str(root))
    got = ck.kinds_read(str(root))
    assert not got.get("docx"), "старый конфиг получил Word-статусы без Word-полей"
    assert ck.kinds_write(str(root), got["kinds"], got["digest"]).get("ok")
    assert _mod("make_kinds").read_kinds(str(root)) == before, "форма потеряла старый конфиг"
    assert "docx_" not in cfg.read_text(encoding="utf-8"), "запись добавила docx-поля в старый конфиг"

    ui = panel_sources()
    render = _js_function(ui, "async function renderKinds(")
    draw = render[render.index("const drawDocx ="):render.index("const docxBuild =")]
    assert "textContent = docxStatusText" in draw
    assert "innerHTML" not in draw and "html:" not in draw
    assert ".detail" not in draw, "техническая причина сборки попала в HTML-подпись"
