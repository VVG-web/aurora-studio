"""Проверки движка Aurora, часть 144. Каркас и помощники — tests/harness.py.

Кнопка публикации в «Файлах» видна и честна (выпуск A «Confluence»): состояние публикации считает сервер
(`file_read` → `publish`), а не глобал страницы, который перестали присваивать при переносе раздела в модуль;
метка «страница отстала» идёт по отпечатку текста (`published_sha`), а не по коммиту, по которому горела всегда;
открытый в редакторе файл перечитывается после публикации. Confluence — подставной сервер на 127.0.0.1, настоящей
записи нет; проекты — только во временных папках.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, _js_function, make_project, panel_sources, test

MARK = "<!-- ниже — производство, в чистовик не идёт -->"
BODY = "# Итог\n\nПлатёж проводится за пять дней.\n"
KITCHEN = "\n## Допущения\n\n- срок взят из практики\n"
WIKI = "https://wiki.example.com"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _ck():
    _mod("aurora_common")
    if str(KIT / "cockpit") not in sys.path:
        sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


def _doc(head: str = "type: us\nstatus: ready\n", body: str = BODY, kitchen: str = KITCHEN) -> str:
    return f"---\n{head}---\n\n{body}\n{MARK}\n{kitchen}"


def _project(tmp: Path, kinds: str = "", base_url: str = WIKI, git: bool = False, sub: str = "") -> Path:
    """Проект с адресом Confluence и видами артефактов (`kinds` — готовый текст секции `artifacts:`)."""
    if sub:
        tmp = tmp / sub
        tmp.mkdir()
    root = make_project(tmp, git=git)
    cfg = root / "aurora.config.yaml"
    text = cfg.read_text(encoding="utf-8")
    if base_url:
        text = text.replace('    space: "T"\n', f'    space: "T"\n    base_url: "{base_url}"\n')
    cfg.write_text(text + kinds, encoding="utf-8")
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "US.md").write_text("шаблон", encoding="utf-8")
    return root


def _put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


KINDS = ('artifacts:\n'
         '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
         f'    publish_url: "{WIKI}/pages/viewpage.action?pageId=777"\n'
         '  note:\n    title: "Заметка"\n    template: "Templates/US.md"\n    out: "Artifacts/note"\n')


# ------------------------------------------------------------------ состояние кнопки считает сервер

@test
def test_file_read_tells_whether_an_artifact_can_be_published(tmp: Path):
    """`file_read` отдаёт `publish`: кнопка не зависит от глобалов страницы (так она пропала в 1.126.0).

    Файл в папке результата вида → `ready`/`blocked` (серая, с причиной и местом починки); чужая папка, закрытый слой и
    не-markdown → `none`; родитель — по тому же порядку, что у `ship:publish`; `type:` шапки важнее папки."""
    ck = _ck()
    root = _project(tmp, KINDS)
    _put(root, "Artifacts/us/US-1.md", _doc())
    _put(root, "Artifacts/note/N-1.md", _doc("type: note\nstatus: ready\n"))
    _put(root, "Artifacts/note/N-2.md", _doc("type: note\npublish_parent: \"888\"\n"))
    _put(root, "Artifacts/note/N-3.md", _doc("type: note\nconfluence_page_id: 5\n"))
    _put(root, "Artifacts/note/N-4.md", _doc("type: us\n"))               # type важнее папки
    _put(root, "Artifacts/us/заметка.txt", "не markdown")
    _put(root, "Workspaces/черновик.md", _doc())

    def pub(rel: str) -> dict:
        d = ck.file_read(str(root), rel)
        assert "error" not in d, (rel, d)
        return d["publish"]

    p = pub("Artifacts/us/US-1.md")
    assert p["state"] == "ready" and p["kind"] == "us" and p["action"] == "create", p
    assert p["parent_from"] == "publish_url" and p["parent"].endswith("pageId=777"), p
    assert p["why"] == "" and p["fix"] == "" and p["prompt"] == "" and p["gate"] == [], p

    p = pub("Artifacts/note/N-1.md")
    assert p["state"] == "blocked" and p["fix"] == "kinds" and "родительская страница" in p["why"], \
        f"вид без родителя: кнопка не серая или не называет, где чинить: {p}"
    p = pub("Artifacts/note/N-2.md")
    assert p["state"] == "ready" and p["parent_from"] == "publish_parent" and p["parent"] == "888", p
    p = pub("Artifacts/note/N-3.md")
    assert p["state"] == "ready" and p["action"] == "update" and p["page"] == "5", \
        f"страница уже есть: родитель не нужен, публикация обновит её: {p}"
    p = pub("Artifacts/note/N-4.md")
    assert p["kind"] == "us" and p["state"] == "ready", f"type: шапки не важнее папки: {p}"

    for rel in ("Artifacts/us/заметка.txt", "Workspaces/черновик.md"):
        assert ck.file_read(str(root), rel).get("publish", {}).get("state") == "none", \
            f"кнопка у файла, который не артефакт вида: {rel}"
    kb = _put(root, "AuroraKnowledgeDB/Concepts/Карточка.md", "---\ntype: concept\nstatus: knowledge\n---\n\n# К\n")
    raw = _put(root, "Raw/customer/договор.md", _doc())
    for path in (kb, raw):
        rel = path.relative_to(root).as_posix()
        assert ck.file_read(str(root), rel).get("publish", {}).get("state") == "none", \
            f"закрытый слой получил кнопку публикации: {rel}"


@test
def test_publish_button_is_blocked_with_a_reason_when_the_project_cannot_publish(tmp: Path):
    """Нет адреса или пространства Confluence → серая кнопка и путь «Настройки проекта»; `Deliverables/work` без вида —
    родитель из конфига; ворота ревью называются кодами, но кнопку не гасят."""
    ck = _ck()
    root = _project(tmp, KINDS, base_url="")
    _put(root, "Artifacts/us/US-1.md", _doc())
    p = ck.file_read(str(root), "Artifacts/us/US-1.md")["publish"]
    assert p["state"] == "blocked" and p["fix"] == "project" and "адрес Confluence" in p["why"], p

    work = _project(tmp, "", sub="w")
    _put(work, "Deliverables/work/ОПЗ.md", _doc("type: opz\n"))
    p = ck.file_read(str(work), "Deliverables/work/ОПЗ.md")["publish"]
    assert p["state"] == "blocked" and p["fix"] == "project" and p["kind"] == "", \
        f"документ без вида и без родителя в конфиге: {p}"
    cfg = work / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(
        '    base_url:', '    publish_parent: "9001"\n    base_url:'), encoding="utf-8")
    p = ck.file_read(str(work), "Deliverables/work/ОПЗ.md")["publish"]
    assert p["state"] == "ready" and p["parent_from"] == "confluence" and p["parent"] == "9001", p

    gated = _project(tmp, KINDS.replace('    publish_url:', '    review: "us"\n    publish_prompt: "Prompts/us_publish.md"\n'
                                        '    publish_url:', 1), sub="g")
    _put(gated, "Artifacts/us/US-1.md", _doc())
    p = ck.file_read(str(gated), "Artifacts/us/US-1.md")["publish"]
    assert p["state"] == "ready" and p["review"] == "us" and "review-missing" in p["gate"], p
    assert p["prompt"] == "Prompts/us_publish.md", f"промпт публикации вида не дошёл до страницы: {p}"


# ------------------------------------------------------------------ метка «страница отстала»

@test
def test_stale_mark_follows_the_text_that_goes_out_not_the_commit(tmp: Path):
    """`published_sha` — отпечаток чистовика: сразу после публикации метка не горит, после правки текста — горит.

    По коммиту метка горела у каждого опубликованного файла: на коммите публикации файл не содержит самого штампа."""
    P, AC, ck = _mod("publish_doc"), _mod("aurora_common"), _ck()
    root = _project(tmp, KINDS, git=True)
    text = _doc()
    rel = "Artifacts/us/US-1.md"
    path = _put(root, rel, text)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *a],  # noqa: E731
                                    cwd=str(root), check=True, capture_output=True, text=True, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "документ")
    commit = git("rev-parse", "--short", "HEAD").stdout.strip()

    sha = AC.clean_sha(text)
    stamped = P.stamp(text, "4242", commit, "1", WIKI + "/pages/viewpage.action?pageId=4242", sha)
    assert AC.frontmatter(stamped)["published_sha"] == sha and len(sha) == 16, stamped[:400]
    assert AC.clean_sha(stamped) == sha, "штамп меняет отпечаток того, что им проштамповано"
    path.write_text(stamped, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "штамп публикации")

    d = ck.file_read(str(root), rel)
    assert d["published"] and d["stale"] is False and d["stale_known"] is True, \
        f"сразу после публикации страница названа отставшей: {d['stale']}"
    assert ck.file_changed_since_publish(str(root), rel, {"published_commit": commit}) is True, \
        "сравнение по коммиту снова честное — тогда отпечаток не нужен; проверьте, что изменилось"

    def stale_after(new: str) -> bool:
        path.write_text(new, encoding="utf-8", newline="")
        return ck.file_read(str(root), rel)["stale"]

    assert stale_after(stamped.replace("пять дней", "три дня")) is True, "правка чистовика не сделала страницу отставшей"
    assert stale_after(stamped.replace("срок взят из практики", "срок уточнён")) is False, \
        "правка допущений (ниже границы чистовика) считается правкой страницы"
    assert stale_after(stamped.replace("status: ready", "status: ready\nreview: us")) is False, \
        "правка шапки считается правкой страницы"
    assert stale_after(stamped.replace("\n", "\r\n")) is False, "перевод строк считается правкой страницы"
    assert stale_after(stamped.replace("Итог", "Итог  ")) is False, "лишние пробелы считаются правкой страницы"

    # опубликован прежней версией кита: отпечатка нет — честно «неизвестно», а не метка, горевшая всегда
    old = P.stamp(text, "4242", commit, "1", WIKI + "/pages/viewpage.action?pageId=4242")
    assert "published_sha" not in AC.frontmatter(old), "без отпечатка штамп всё равно записал поле"
    path.write_text(old.replace("пять дней", "три дня"), encoding="utf-8")
    d = ck.file_read(str(root), rel)
    assert d["stale"] is False and d["stale_known"] is False, f"старый штамп: {d['stale']} {d['stale_known']}"
    # и не опубликованный вовсе
    path.write_text(text, encoding="utf-8")
    d = ck.file_read(str(root), rel)
    assert d["published"] == "" and d["stale"] is False and d["stale_known"] is True, d


@test
def test_review_and_publication_share_one_fingerprint_of_the_clean_text(tmp: Path):
    """Отпечаток ревью (`review_sha`) и публикации (`published_sha`) считает один код: «тот же ли текст уходит наружу»."""
    AC, RR = _mod("aurora_common"), _mod("review_run")
    text = _doc()
    assert RR.subject_sha(text) == AC.clean_sha(text), "отпечатки разошлись"
    assert AC.clean_sha(text) == AC.clean_sha(text.replace("status: ready", "status: draft")), "шапка вошла в отпечаток"
    assert AC.clean_sha(text) != AC.clean_sha(text.replace("пять", "три")), "правка чистовика не меняет отпечаток"
    assert AC.clean_sha(text) == AC.clean_sha(text.replace("практики", "опыта")), "кухня вошла в отпечаток"


# ------------------------------------------------------------------ подставной Confluence

class _Wiki:
    """Подставной Confluence на 127.0.0.1: страницы в памяти, журнал запросов. Настоящей записи нет."""

    def __init__(self):
        self.pages: dict = {}
        self.log: list = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                outer.log.append(("GET", u.path, None))
                parts = u.path.strip("/").split("/")
                if len(parts) == 4:                                         # rest/api/content/<id>
                    page = outer.pages.get(parts[3])
                    return self._send(200, page) if page else self._send(404, {"message": "нет страницы"})
                title = urllib.parse.parse_qs(u.query).get("title", [""])[0]
                self._send(200, {"results": [p for p in outer.pages.values() if p["title"] == title]})

            def _body(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")

            def do_POST(self):
                payload = self._body()
                outer.log.append(("POST", self.path, payload))
                pid = str(4242 + len(outer.pages))
                outer.pages[pid] = {"id": pid, "title": payload["title"], "version": {"number": 1},
                                    "body": payload["body"], "ancestors": payload.get("ancestors", [])}
                self._send(200, {"id": pid, "version": {"number": 1}})

            def do_PUT(self):
                payload = self._body()
                outer.log.append(("PUT", self.path, payload))
                pid = self.path.strip("/").split("/")[-1]
                page = outer.pages[pid]
                if payload["version"]["number"] != page["version"]["number"] + 1:
                    return self._send(409, {"message": "версия"})
                page.update(title=payload["title"], body=payload["body"], version={"number": payload["version"]["number"]})
                self._send(200, {"id": pid, "version": page["version"]})

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def writes(self) -> list:
        return [(m, p, b) for m, p, b in self.log if m in ("POST", "PUT")]


def _publish(root: Path, rel: str, *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "CONFLUENCE_PAT": "test-token-not-real", "CONFLUENCE_USER": "", "CONFLUENCE_PASSWORD": ""}
    return subprocess.run([sys.executable, str(SCRIPTS / "publish_doc.py"), rel, "--apply", *extra], cwd=str(root),
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")


@test
def test_publishing_stamps_the_fingerprint_and_the_parent_comes_from_the_kind(tmp: Path):
    """Публикация на подставном сервере: родитель — из `publish_url` вида (документ без `publish_parent` в шапке),
    в штамп ложится `published_sha`, метка не горит; правка чистовика зажигает её, повторная публикация гасит."""
    AC, ck = _mod("aurora_common"), _ck()
    wiki = _Wiki()
    try:
        kinds = KINDS.replace(f"{WIKI}/pages/viewpage.action?pageId=777", f"{wiki.url}/pages/viewpage.action?pageId=777")
        root = _project(tmp, kinds, base_url=wiki.url)
        rel = "Artifacts/us/US-1.md"
        text = _doc()
        path = _put(root, rel, text)
        before = ck.file_read(str(root), rel)
        assert before["publish"]["state"] == "ready", before["publish"]

        cp = _publish(root, rel)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        (method, _p, payload), = wiki.writes()
        assert method == "POST" and payload["ancestors"] == [{"id": "777"}] and payload["title"] == "Итог", \
            f"родитель не взят у вида: {payload.get('ancestors')}"
        assert "Допущения" not in payload["body"]["storage"]["value"], "кухня производства уехала на страницу"
        fm = AC.frontmatter(path.read_text(encoding="utf-8"))
        assert fm["confluence_page_id"] == "4242" and fm["published_version"] == "1", fm
        assert fm["published_sha"] == AC.clean_sha(text), f"в штамп не лёг отпечаток чистовика: {fm}"
        after = ck.file_read(str(root), rel)
        assert after["stale"] is False and after["stale_known"] is True, "сразу после публикации страница отстала"
        assert after["publish"]["action"] == "update" and after["publish"]["page"] == "4242", after["publish"]

        # открытый в редакторе файл переписан скриптом: прежний слепок даёт конфликт, перечитанный — нет (дефект 2.3)
        assert after["digest"] != before["digest"], "публикация не изменила файл — нечего перечитывать"
        stale_save = ck.file_write(str(root), rel, before["text"], expect=before["digest"])
        assert stale_save.get("conflict") is True, "сохранение со старым слепком затёрло штамп публикации"
        again = ck.file_write(str(root), rel, after["text"], expect=after["digest"])
        assert again.get("ok") and AC.frontmatter(path.read_text(encoding="utf-8"))["published_sha"] == fm["published_sha"], \
            "сохранение после перечитывания потеряло штамп"

        # правка чистовика → «отстала»; правка кухни → нет; повторная публикация гасит метку
        path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
        assert ck.file_read(str(root), rel)["stale"] is True
        path.write_text(path.read_text(encoding="utf-8").replace("три дня", "пять дней"), encoding="utf-8")
        assert ck.file_read(str(root), rel)["stale"] is False, "возврат текста не снял метку"
        path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "четыре дня"), encoding="utf-8")
        cp = _publish(root, rel)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert [m for m, _p, _b in wiki.writes()] == ["POST", "PUT"], wiki.writes()
        d = ck.file_read(str(root), rel)
        assert d["stale"] is False and AC.frontmatter(d["text"])["published_version"] == "2", d["text"][:300]
        assert AC.frontmatter(d["text"])["published_sha"] == AC.clean_sha(d["text"]), "отпечаток не обновился"
    finally:
        wiki.close()


@test
def test_header_parent_beats_the_kind_and_the_kind_beats_the_config(tmp: Path):
    """Порядок родителя: `--parent` → шапка → `publish_url` вида → конфиг; вид — только там, где иначе родителя нет."""
    P = _mod("publish_doc")
    assert P.choose_parent("1", {"publish_parent": "2"}, {"publish_parent": "4"}, "3") == ("1", "--parent")
    assert P.choose_parent("", {"publish_parent": "2"}, {"publish_parent": "4"}, "3")[0] == "2"
    assert P.choose_parent("", {}, {"publish_parent": "4"}, "3") == ("3", "publish_url вида артефакта")
    assert P.choose_parent("", {}, {"publish_parent": "4"}) == ("4", "confluence.publish_parent в конфиге")
    assert P.choose_parent("", {}, {}, "") == ("", "")


# ------------------------------------------------------------------ страница: рисует ответ сервера

def _fn(ui: str, head: str) -> str:
    """Функция панели целиком (`_js_function` отдаёт её без закрывающей скобки)."""
    return _js_function(ui, head) + "\n}\n"


def _node(code: str, tmp: Path) -> dict:
    node = shutil.which("node")
    assert node, "нет node: поведение страницы проверить нечем — поставьте его, иначе регрессия доедет до человека"
    src = tmp / "case.mjs"
    src.write_text(code, encoding="utf-8")
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[-1500:]
    return json.loads(cp.stdout)


@test
def test_panel_draws_the_publish_state_the_server_gave(tmp: Path):
    """Страница рисует ответ сервера (`publishView`), а не гадает: нет `publish` — кнопки нет, `blocked` — серая с причиной."""
    ui = panel_sources()
    view = _fn(ui, "function publishView(")
    out = _node(view + """
const rows = {
  ready: publishView({publish: {state: "ready", gate: ["review-missing"]}, stale: true, stale_known: true, published: "2026-10-01"}),
  blocked: publishView({publish: {state: "blocked", why: "нет родителя", fix: "kinds", gate: ["x"]}}),
  none: publishView({publish: {state: "none"}}),
  oldServer: publishView({}),
  legacy: publishView({published: "2026-10-01", stale: false, stale_known: false, publish: {state: "ready"}}),
  fresh: publishView({published: "", stale_known: true, publish: {state: "ready"}}),
};
console.log(JSON.stringify(rows));
""", tmp)
    assert out["ready"] == {"button": "ready", "why": "", "fix": "", "gate": ["review-missing"], "stale": True, "unknown": False}, out
    assert out["blocked"]["button"] == "blocked" and out["blocked"]["why"] == "нет родителя" and out["blocked"]["fix"] == "kinds" \
        and out["blocked"]["gate"] == [], out
    assert out["none"]["button"] == "hidden" and out["oldServer"]["button"] == "hidden", out
    assert out["legacy"]["unknown"] is True and out["legacy"]["stale"] is False, out
    assert out["fresh"]["unknown"] is False and out["fresh"]["stale"] is False, out
    # файл открывается через это состояние, а не через глобал страницы
    assert "drawPublish(d)" in _js_function(ui, "async function openFile(") and "S.kinds" not in ui
    assert "publishTarget" not in ui, "прежняя догадка по виду вернулась"


@test
def test_open_file_is_reread_after_a_successful_publication(tmp: Path):
    """После `ship:publish` (из «Файлов» или «Продуктивности») открытый файл перечитывается: скрипт переписал его штампом.

    На скрытом редакторе перечитывание ждёт возвращения в «Файлы»; несохранённое не затирается; чужой файл не трогается."""
    ui = panel_sources()
    after = _fn(ui, "async function afterPublish(")
    out = _node("""
const calls = [], toasts = [];
let S = {view: "console"};
let F = {path: "Artifacts/us/US-1.md", dirty: false, files: [{path: "Artifacts/us/US-1.md", name: "US-1.md", text: true}], reopen: ""};
const openFile = async f => { calls.push(f.path); };
const toast = (m, k) => toasts.push([m, k]);
const t = k => k;
""" + after + """
const rel = "Artifacts/us/US-1.md";
const log = {};
await afterPublish(rel);                                   // «Файлы» скрыты: ждём возвращения
log.hidden = {calls: calls.length, reopen: F.reopen};
S.view = "files";
await afterPublish(F.reopen);                              // вернулись: перечитываем
log.visible = {calls: calls.length, reopen: F.reopen};
await afterPublish("Artifacts/us/другой.md");              // открыт не тот файл
log.other = calls.length;
F.dirty = true;
await afterPublish(rel);                                   // в редакторе несохранённое
log.dirty = {calls: calls.length, toasts: toasts.length};
console.log(JSON.stringify(log));
""", tmp)
    assert out["hidden"] == {"calls": 0, "reopen": "Artifacts/us/US-1.md"}, out
    assert out["visible"] == {"calls": 1, "reopen": ""}, out
    assert out["other"] == 1 and out["dirty"] == {"calls": 1, "toasts": 1}, out
    poll = _js_function(ui, "async function poll(")
    assert 'rc === 0 && String(label).startsWith("ship:publish ")' in poll and "afterPublish(" in poll, \
        "завершение публикации не перечитывает открытый файл"
    assert "F.reopen" in _js_function(ui, "async function renderFiles("), \
        "перечитывание, отложенное до возвращения в «Файлы», некому выполнить"
    assert 'poll(res.job, 0, "ship:publish " + F.path)' in _js_function(ui, "async function publishFromEditor("), \
        "метка публикации из редактора разошлась с той, по которой ищется файл"
    assert '"ship:publish " + f.rel' in panel_sources(), "метка публикации из «Продуктивности» разошлась"


@test
def test_publish_button_is_drawn_from_the_state_and_leads_to_the_fix(tmp: Path):
    """`drawPublish` на настоящем коде страницы: готово — кнопка включена; не готово — серая, причина рядом и кнопка
    в нужные настройки; неприменимо — кнопки нет; метка «отстала» не даёт публикации, которая невозможна."""
    ui = panel_sources()
    code = _fn(ui, "function publishView(") + _fn(ui, "function drawPublish(")
    out = _node("""
const nodes = {};
const $ = sel => nodes[sel] || (nodes[sel] = {hidden: true, disabled: false, title: "", className: "", items: [],
  set innerHTML(v){ this.items = []; }, get innerHTML(){ return ""; }, append(...a){ this.items.push(...a); }});
const el = (tag, attrs, ...kids) => ({tag, attrs: attrs || {}, kids});
const t = (k, v) => k + (v ? JSON.stringify(v) : "");
const F = {};
const SGROUPS = {focus: ""};
let shown = "";
const show = v => { shown = v; };
const publishFromEditor = () => {};
""" + code + """
const snap = () => ({pub: {hidden: nodes["#filePub"].hidden, disabled: nodes["#filePub"].disabled, title: nodes["#filePub"].title},
  note: {hidden: nodes["#filePubNote"].hidden, cls: nodes["#filePubNote"].className, items: nodes["#filePubNote"].items.length},
  stale: {hidden: nodes["#fileStale"].hidden, items: nodes["#fileStale"].items.length}, publishable: F.publishable});
const log = {};
drawPublish({publish: {state: "ready", gate: []}});
log.ready = snap();
drawPublish({publish: {state: "blocked", why: "нет родителя", fix: "kinds"}, published: "2026-10-01", stale: true, stale_known: true});
log.blocked = snap();
nodes["#filePubNote"].items[1].attrs.onclick();
log.jump = {focus: SGROUPS.focus, shown};
log.staleBtnDisabled = nodes["#fileStale"].items[1].attrs.disabled;
drawPublish({publish: {state: "none"}});
log.none = snap();
drawPublish({publish: {state: "ready", gate: ["review-stale"]}, published: "2026-10-01", stale: true, stale_known: true});
log.gate = snap();
log.staleBtnEnabled = nodes["#fileStale"].items[1].attrs.disabled;
drawPublish({publish: {state: "ready"}, published: "2026-10-01", stale: false, stale_known: false});
log.unknown = snap();
console.log(JSON.stringify(log));
""", tmp)
    assert out["ready"] == {"pub": {"hidden": False, "disabled": False, "title": ""},
                            "note": {"hidden": True, "cls": "note", "items": 0},
                            "stale": {"hidden": True, "items": 0}, "publishable": True}, out["ready"]
    assert out["blocked"]["pub"] == {"hidden": False, "disabled": True, "title": "нет родителя"} \
        and out["blocked"]["note"] == {"hidden": False, "cls": "note warn", "items": 2} and out["blocked"]["publishable"] is False, out["blocked"]
    assert out["jump"] == {"focus": "project:kinds", "shown": "project"}, f"серая кнопка не ведёт в настройки видов: {out['jump']}"
    assert out["staleBtnDisabled"] == "", "метка «отстала» даёт нажать публикацию, которая невозможна"
    assert out["none"]["pub"]["hidden"] is True and out["none"]["note"]["hidden"] is True and out["none"]["publishable"] is False, out["none"]
    assert out["gate"]["pub"]["disabled"] is False and out["gate"]["note"]["hidden"] is False and out["gate"]["note"]["cls"] == "note", \
        f"ворота ревью погасили кнопку или промолчали: {out['gate']}"
    assert out["staleBtnEnabled"] is None, "при готовой публикации кнопка в метке «отстала» выключена"
    assert out["unknown"]["stale"] == {"hidden": False, "items": 1}, f"старый штамп: метка молчит, а не говорит «неизвестно»: {out['unknown']}"
