"""Проверки движка Aurora, часть 150. Каркас и помощники — tests/harness.py, подставной Confluence — part_148.py.

Выпуск B «Confluence»: штамп и настройки. Штамп публикации пишется атомарно и сохраняет метку порядка байтов и окончания
строк (CRLF) документа; в нём — `published_input`, `published_space`, `published_parent`. Создание документа агентом
больше не пишет `publish_parent` в шапку. Форма видов пишет `publish_path`/`publish_title` и не стирает чужого:
незнакомые поля вида и комментарии внутри секции `artifacts:` остаются; устаревший слепок — 409. Панель: состояние кнопки
в «Файлах» и «Продуктивности» считается одним кодом, пример пути для формы (`/api/kinds/example`) и план публикации
(`/api/publish/plan`) считает сервер. Проекты — во временных папках; сеть — только подставной сервер на 127.0.0.1.
"""
from __future__ import annotations

import http.client
import importlib
import json
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

from cases import part_148 as W
from harness import KIT, SCRIPTS, make_project, set_home, test

BOM = "﻿"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


ENV_FILE = _mod("aurora_common").ENV_FILE      # имя файла машины проекта — у движка, а не копией в проверке


def _ck():
    _mod("aurora_common")
    if str(KIT / "cockpit") not in sys.path:
        sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


# ------------------------------------------------------------------ штамп

@test
def test_the_stamp_is_atomic_and_keeps_the_bom_and_crlf(tmp: Path):
    """Документ с BOM и CRLF (сохранён на Windows): после публикации — та же метка порядка байтов и только CRLF, новые поля
    штампа на месте, временных файлов рядом нет; тело не изменилось ни на знак."""
    AC = _mod("aurora_common")
    wiki = W.Wiki()
    try:
        rid = W.root_page(wiki)
        root = W.project(tmp, wiki.url, W.KIND_US.format(root=rid))
        text = W.doc("type: us\nstatus: ready\nrelease: \"1.0\"\n")
        path = root / "Artifacts" / "us" / "US-1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((BOM + text.replace("\n", "\r\n")).encode("utf-8"))
        cp, res = W.publish(root, "Artifacts/us/US-1.md", "--apply")
        assert cp.returncode == 0 and res["outcome"] == "ok" and res["stamped"] is True, cp.stdout + cp.stderr
        raw = path.read_bytes().decode("utf-8")
        assert raw.startswith(BOM) and raw.count(BOM) == 1, "метка порядка байтов потеряна или удвоена"
        assert "\n" not in raw.replace("\r\n", ""), "окончания строк перемешаны: штамп записан LF в CRLF-файле"
        fm = AC.frontmatter(raw.lstrip(BOM).replace("\r\n", "\n"))
        for key in ("confluence_page_id", "published_version", "published_sha", "published_input", "published_space",
                    "published_parent"):
            assert fm.get(key), f"в штампе нет {key}: {fm}"
        assert fm["published_space"] == W.SPACE and fm["published_sha"] == AC.clean_sha(text), fm
        assert AC.body(raw.lstrip(BOM).replace("\r\n", "\n")) == AC.body(text), "штамп тронул тело документа"
        assert not [p.name for p in path.parent.iterdir() if p.name != "US-1.md"], "рядом остался временный файл"
        # и повтор узнаёт тот же файл: CRLF и BOM не делают его «другим»
        _cp, res = W.publish(root, "Artifacts/us/US-1.md", "--apply")
        assert res["outcome"] == "unchanged", res["outcome"]
    finally:
        wiki.close()


@test
def test_creation_no_longer_writes_the_parent_into_the_header(tmp: Path):
    """Агент создания кладёт документ с шапкой этапов, но без `publish_parent`: место страницы берётся из вида в момент
    публикации, и смена настройки действует на уже созданные документы."""
    R = _mod("agent_runner")
    AC = _mod("aurora_common")
    root = make_project(tmp)
    st = {"kind": "us", "idea": "приём заявки", "sid": "s-b", "stages": {k: "2026-10-01" for k in R.MAKE_STAGES},
          "spec": {"title": "История", "out": "Artifacts/us", "template": "t",
                   "publish_url": "https://wiki.example.com/pages/viewpage.action?pageId=7"},
          "plan": "1. Раздел", "draft": "# История\n\nТекст.", "issues": [],
          "momus": {"ok": True, "clean": True, "unsupported": 0, "text": "ВЕРДИКТ: ОПОРА ЕСТЬ"}}
    text = (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")
    assert "publish_parent" not in AC.frontmatter(text) and "publish_parent" not in text, text[:500]
    assert "publish_parent:" not in (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ форма видов: не стирает чужое

CONFIG = (
    'project:\n  name: "Т"\n\n'
    'artifacts:\n'
    '  # виды проекта: правит форма «Артефакты»\n'
    '  us:\n'
    '    title: "История"\n'
    '    # шаблон согласован с командой\n'
    '    template: Templates/US.md\n'
    '    out: Artifacts/us\n'
    '    owner_note: "чужое поле, форма его не знает"\n'
    '    task:\n      project: PRJ\n      sprint: "чужое свойство задачи"\n'
    '  # старый вид, уберём\n'
    '  old:\n    title: "Старый"\n    template: Templates/US.md\n    out: Artifacts/old\n'
    '\n'
    'cards:\n  x: 1\n'
)


@test
def test_the_kinds_form_writes_the_path_fields_and_keeps_what_it_does_not_know(tmp: Path):
    """Круг «форма → kinds_write → read_kinds» для `publish_path` и `publish_title`; незнакомое поле вида, незнакомое
    свойство задачи и комментарии внутри секции переживают запись; убранный вид уходит с комментарием над ним; повтор —
    байт в байт; запись по устаревшему слепку — 409 и файл не тронут."""
    MK, ck = _mod("make_kinds"), _ck()
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "US.md").write_text("шаблон", encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(CONFIG, encoding="utf-8")
    state = ck.kinds_read(str(root))
    kinds = json.loads(json.dumps(state["kinds"]))
    assert kinds["us"]["publish_path"] == "" and kinds["us"]["publish_title"] == "", kinds["us"]
    kinds["us"].update(publish_url="https://wiki.example.com/display/PRJ/US", publish_path="Релиз {release} > {epic?}",
                       publish_title="[US] {title}")
    del kinds["old"]
    res = ck.kinds_write(str(root), kinds, state["digest"])
    assert res.get("ok"), res
    text = cfg.read_text(encoding="utf-8")
    back = MK.read_kinds(str(root))
    assert back["us"]["publish_path"] == "Релиз {release} > {epic?}" and back["us"]["publish_title"] == "[US] {title}", back["us"]
    for kept in ('owner_note: "чужое поле, форма его не знает"', 'sprint: "чужое свойство задачи"',
                 "# виды проекта: правит форма «Артефакты»", "# шаблон согласован с командой"):
        assert kept in text, f"запись формы стёрла «{kept}»:\n{text}"
    assert "old:" not in text and "# старый вид, уберём" not in text, "убранный вид или его комментарий остались"
    assert text.endswith("\ncards:\n  x: 1\n") and text.startswith('project:\n  name: "Т"\n\nartifacts:\n'), text
    assert back["us"]["task"] == {"project": "PRJ"}, back["us"]["task"]
    # повтор того же — байт в байт
    again = ck.kinds_write(str(root), json.loads(json.dumps(ck.kinds_read(str(root))["kinds"])), res["digest"])
    assert again.get("ok") and cfg.read_text(encoding="utf-8") == text, "повторная запись изменила файл"
    # устаревший слепок — 409, файл не тронут
    cfg.write_text(text.replace("[US] {title}", "[US] {title} (руками)"), encoding="utf-8")
    stale = ck.kinds_write(str(root), kinds, res["digest"])
    assert stale.get("conflict") is True and cfg.read_text(encoding="utf-8").count("(руками)") == 1, stale
    # поле, которого запись не прислала, она не стирает
    partial = {"us": {k: v for k, v in back["us"].items() if k != "publish_title"}}
    assert ck.kinds_write(str(root), partial).get("ok")
    assert MK.read_kinds(str(root))["us"]["publish_title"] == "[US] {title} (руками)"


# ------------------------------------------------------------------ панель: одно состояние, пример, план

class _Panel:
    """Сервер панели на временном ките: проект в обнаруживаемой папке, запросы — настоящими HTTP."""

    def __init__(self, tmp: Path):
        self.restore = set_home(tmp / "home")
        self.ck = _ck()
        self.kit = tmp / "kit"
        (self.kit / "local").mkdir(parents=True)
        self.old_kit = self.ck.KIT
        self.ck.KIT = str(self.kit)
        self.roots = tmp / "roots"
        self.roots.mkdir()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.ck.Handler)
        self.srv.roots = [str(self.roots)]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=120)
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
        self.ck.KIT = self.old_kit
        self.restore()


@test
def test_files_and_productivity_share_one_publish_state(tmp: Path):
    """«Продуктивность» берёт состояние кнопки у того же `publish_info`, что «Файлы»: вид без `publish_url` с корнем в
    шапке документа или в конфиге — публикуется (раньше кнопку гасило отсутствие `publish_url` у вида)."""
    ck = _ck()
    root = W.project(tmp, "https://wiki.example.com",
                     'artifacts:\n  note:\n    title: "Заметка"\n    template: "Templates/US.md"\n    out: "Artifacts/note"\n')
    W.put(root, "Artifacts/note/N-1.md", W.doc("type: note\nstatus: ready\npublish_parent: \"55\"\n"))
    W.put(root, "Artifacts/note/N-2.md", W.doc("type: note\nstatus: ready\n"))
    files = {f["name"]: f["publish"] for f in ck.artifact_files(str(root), "note")}
    for name in ("N-1.md", "N-2.md"):
        assert files[name] == ck.file_read(str(root), f"Artifacts/note/{name}")["publish"], f"{name}: состояния разошлись"
    assert files["N-1.md"]["state"] == "ready" and files["N-1.md"]["parent_from"] == "publish_parent", files["N-1.md"]
    assert files["N-2.md"]["state"] == "blocked" and files["N-2.md"]["fix"] == "kinds", files["N-2.md"]
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace('    base_url:', '    publish_parent: "9"\n    base_url:'), encoding="utf-8")
    assert ck.artifact_files(str(root), "note")[1]["publish"]["state"] == "ready", "корень из конфига не принят"
    # без адреса Confluence — серая с причиной «Настройки проекта»; пространства в конфиге больше не нужно
    cfg.write_text(cfg.read_text(encoding="utf-8").replace('    space: "T"\n', ""), encoding="utf-8")
    assert ck.file_read(str(root), "Artifacts/note/N-1.md")["publish"]["state"] == "ready", "без confluence.space кнопка гаснет"


@test
def test_the_server_counts_the_example_path_and_the_publish_plan(tmp: Path):
    """`GET /api/kinds/example` — пример пути для формы по последнему документу вида и по значениям формы (несохранённым
    тоже), на языке страницы; `POST /api/publish/plan` — план сухим прогоном `ship:publish --json` на подставном сервере,
    ничего не записав."""
    panel = _Panel(tmp)
    wiki = W.Wiki()
    try:
        rid = W.root_page(wiki)
        root = W.project(panel.roots, wiki.url, W.KIND_US.format(root=f"{wiki.url}/pages/viewpage.action?pageId={rid}"))
        W.put(root, "Artifacts/us/US-1.md", W.doc("type: us\nstatus: ready\nrelease: \"1.0\"\nepic: PRJ-1\n"))
        # Поддельный токен — в файле машины ВРЕМЕННОГО проекта: панель секреты в окружение команд не кладёт, их читает
        # сам скрипт. Настоящий файл машины проверка не видит и не трогает.
        (root / ENV_FILE).write_text("CONFLUENCE_PAT=test-token-not-real\n", encoding="utf-8")
        project = str(root)
        q = urllib.parse.urlencode({"project": project, "kind": "us"})
        st, ex = panel.call("GET", "/api/kinds/example?" + q)
        assert st == 200 and ex["doc"] == "Artifacts/us/US-1.md" and ex["root"] == f"#{rid}", ex
        assert ex["levels"] == ["Релиз 1.0", "PRJ-1"] and ex["title"] == "[US] Итог" and ex["missing"] == [], ex
        # значения формы, ещё не сохранённые
        q = urllib.parse.urlencode({"project": project, "kind": "us", "publish_path": "{project} > {sprint}",
                                    "publish_title": "{file}", "publish_url": "https://wiki.example.com/display/DOC/Корень"})
        st, ex = panel.call("GET", "/api/kinds/example?" + q)
        assert ex["root"] == "Корень" and ex["levels"] == ["Test"] and ex["title"] == "US-1" and ex["missing"] == ["sprint"], ex
        # вид без документов — уровни шаблонами
        q = urllib.parse.urlencode({"project": project, "kind": "us", "out": "Artifacts/пусто", "publish_path": "A > {b}"})
        st, ex = panel.call("GET", "/api/kinds/example?" + q)
        assert ex["doc"] == "" and ex["levels"] == ["A", "{b}"] and ex["title"] == "[US] {title}", ex

        st, res = panel.call("POST", "/api/publish/plan", {"project": project, "path": "Artifacts/us/US-1.md"})
        assert st == 200 and "plan" in res, res
        plan = res["plan"]
        assert plan["outcome"] == "ok" and plan["space"] == W.SPACE and plan["title"] == "[US] Итог", plan
        assert [c["state"] for c in plan["chain"]] == ["create", "create"] and not wiki.writes(), "план что-то записал"
        # причина отказа плана — на языке страницы
        W.put(root, "Artifacts/us/US-2.md", W.doc("type: us\nstatus: ready\n"))
        st, res = panel.call("POST", "/api/publish/plan?lang=en", {"project": project, "path": "Artifacts/us/US-2.md"})
        assert res["plan"]["outcome"] == "blocked" and res["plan"]["code"] == "missing-field", res
        st, res = panel.call("POST", "/api/publish/plan", {"project": project, "path": "Artifacts/us/нет.md"})
        assert "error" in res, res
        # серая кнопка про пустое поле — на английской странице по-английски
        st, got = panel.call("GET", "/api/files/read?lang=en&" + urllib.parse.urlencode({"project": project,
                                                                                           "path": "Artifacts/us/US-2.md"}))
        assert got["publish"]["state"] == "blocked" and got["publish"]["why"].startswith("Can't publish: the page path"), got["publish"]
    finally:
        wiki.close()
        panel.close()
