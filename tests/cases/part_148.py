"""Проверки движка Aurora, часть 148. Каркас и помощники — tests/harness.py.

Выпуск B «Confluence»: путь страницы из настроек вида — цепочка разделов. Корень (`publish_url`) → разделы (`publish_path`,
уровни через `>`, подстановки из шапки документа, `{поле?}` — необязательный уровень) → страница с заголовком по шаблону
(`publish_title`). Пространство — у корня, а не из конфига. Раздела нет — создаётся один раз (только с `--apply`) и
переиспользуется; заголовок, занятый в пространстве под другим родителем, — отказ; обязательная подстановка без значения —
отказ, называющий поле и документ. Сухой прогон с `--json` — план без записи.

Здесь же — подставной Confluence (`Wiki`) для частей 149–151: страницы в памяти, дети постранично (`_links.next`), чтение с
`expand`, создание, обновление с проверкой версии (409), перенос через `ancestors`, разовые ошибки 401/403/404/5xx, обрыв
после POST, правка «человеком» и гонка между проверкой и записью. Настоящей записи в Confluence нет; проекты — во
временных папках, модель не вызывается.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test

MARK = "<!-- ниже — производство, в чистовик не идёт -->"
SPACE = "DOC"                     # пространство корня; в конфиге проекта — другое («T»): брать его нельзя


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _ck():
    _mod("aurora_common")
    if str(KIT / "cockpit") not in sys.path:
        sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


# ------------------------------------------------------------------ подставной Confluence

class Wiki:
    """Подставной Confluence на 127.0.0.1. Страницы в памяти; журнал запросов `log` — (метод, путь, тело).

    Переключатели: `fail` — разовые ответы ошибкой `[(метод, кусок пути, код)]`; `drop_after_post` — заголовки, после
    создания которых соединение рвётся без ответа; `race` — номера страниц, которые «правит человек» между проверкой и
    записью (PUT получает 409); `page_size` — сколько детей в одном ответе (постраничность через `_links.next`);
    `normalize` — сервер дописывает в storage свои атрибуты, как настоящий Confluence (равенства тел не бывает)."""

    def __init__(self):
        self.pages: dict = {}
        self.log: list = []
        self.fail: list = []
        self.drop_after_post: set = set()
        self.race: set = set()
        self.page_size = 2
        self.normalize = True
        self.seq = 100
        self.lock = threading.Lock()
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
                        self._send(code, {"message": f"подставная ошибка {code}"})
                        return True
                return False

            def _body(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                outer.log.append(("GET", self.path, None))
                if self._failed("GET"):
                    return
                parts = u.path.strip("/").split("/")
                with outer.lock:
                    if len(parts) == 6 and parts[4:] == ["child", "page"]:              # rest/api/content/<id>/child/page
                        kids = sorted((p for p in outer.pages.values() if p["parent"] == parts[3]), key=lambda p: int(p["id"]))
                        start = int((q.get("start") or ["0"])[0])
                        limit = min(int((q.get("limit") or ["25"])[0]), outer.page_size)
                        chunk = kids[start:start + limit]
                        out = {"results": [outer.view(p) for p in chunk], "size": len(chunk), "start": start, "limit": limit,
                               "_links": {}}
                        if start + limit < len(kids):
                            out["_links"]["next"] = f"/rest/api/content/{parts[3]}/child/page?limit={limit}&start={start + limit}&expand=version"
                        return self._send(200, out)
                    if len(parts) == 4:                                                  # rest/api/content/<id>
                        page = outer.pages.get(parts[3])
                        return self._send(200, outer.view(page)) if page else self._send(404, {"message": "нет страницы"})
                    title = (q.get("title") or [""])[0]
                    space = (q.get("spaceKey") or [""])[0]
                    hits = [outer.view(p) for p in outer.pages.values() if p["title"] == title and (not space or p["space"] == space)]
                    return self._send(200, {"results": hits, "size": len(hits)})

            def do_POST(self):
                payload = self._body()
                outer.log.append(("POST", self.path, payload))
                if self._failed("POST"):
                    return
                with outer.lock:
                    space = (payload.get("space") or {}).get("key", "")
                    parent = str(((payload.get("ancestors") or [{}])[-1] or {}).get("id") or "")
                    if any(p["title"] == payload["title"] and p["space"] == space for p in outer.pages.values()):
                        return self._send(400, {"message": "A page with this title already exists"})
                    if parent and parent not in outer.pages:
                        return self._send(400, {"message": "нет родителя"})
                    pid = outer.add(payload["title"], parent or None, space, payload["body"]["storage"]["value"], by="Аврора")
                    drop = payload["title"] in outer.drop_after_post
                    outer.drop_after_post.discard(payload["title"])
                if drop:                                     # страница создана, ответ потерян: обрыв после POST
                    self.close_connection = True
                    return
                self._send(200, outer.view(outer.pages[pid]))

            def do_PUT(self):
                payload = self._body()
                outer.log.append(("PUT", self.path, payload))
                if self._failed("PUT"):
                    return
                pid = self.path.split("?")[0].strip("/").split("/")[-1]
                with outer.lock:
                    page = outer.pages.get(pid)
                    if not page:
                        return self._send(404, {"message": "нет страницы"})
                    if pid in outer.race:                    # человек сохранил правку, пока шла публикация
                        outer.race.discard(pid)
                        outer.edit(pid)
                    if payload["version"]["number"] != page["version"] + 1:
                        return self._send(409, {"message": "Version must be incremented on update"})
                    if any(p["title"] == payload["title"] and p["space"] == page["space"] and p["id"] != pid
                           for p in outer.pages.values()):
                        return self._send(400, {"message": "A page with this title already exists"})
                    page.update(title=payload["title"], body=outer.stored(payload["body"]["storage"]["value"]),
                                version=page["version"] + 1, by="Аврора")
                    if payload.get("ancestors"):
                        page["parent"] = str(payload["ancestors"][-1]["id"])
                self._send(200, outer.view(page))

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    # --- данные
    def stored(self, value: str) -> str:
        return value.replace('<ac:structured-macro ac:name=', '<ac:structured-macro ac:schema-version="1" ac:name=') \
            if self.normalize else value

    def add(self, title: str, parent=None, space: str = SPACE, body: str = "", by: str = "Коллега") -> str:
        self.seq += 1
        pid = str(self.seq)
        self.pages[pid] = {"id": pid, "title": title, "space": space, "parent": str(parent) if parent else None,
                           "body": self.stored(body), "version": 1, "by": by, "when": "2026-10-01T10:00:00.000Z"}
        return pid

    def edit(self, pid: str, by: str = "Коллега") -> None:
        page = self.pages[pid]
        page.update(version=page["version"] + 1, body=page["body"] + "<p>правка руками</p>", by=by,
                    when="2026-10-02T11:00:00.000Z")

    def ancestors(self, pid: str) -> list:
        out, cur = [], self.pages[pid]["parent"]
        while cur:
            out.insert(0, {"id": cur, "title": self.pages[cur]["title"], "type": "page"})
            cur = self.pages[cur]["parent"]
        return out

    def view(self, p: dict) -> dict:
        return {"id": p["id"], "type": "page", "title": p["title"], "space": {"key": p["space"]},
                "version": {"number": p["version"], "by": {"displayName": p["by"]}, "when": p["when"]},
                "body": {"storage": {"value": p["body"], "representation": "storage"}},
                "ancestors": self.ancestors(p["id"])}

    # --- что видно снаружи
    def children(self, pid: str) -> list:
        return [p["title"] for p in sorted(self.pages.values(), key=lambda p: int(p["id"])) if p["parent"] == pid]

    def by_title(self, title: str) -> list:
        return [p for p in self.pages.values() if p["title"] == title]

    def writes(self) -> list:
        return [(m, p, b) for m, p, b in self.log if m in ("POST", "PUT")]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ------------------------------------------------------------------ проект и запуск публикации

KIND_US = ('artifacts:\n'
           '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
           '    publish_url: "{root}"\n'
           '    publish_path: "Релиз {{release}} > {{epic?}}"\n'
           '    publish_title: "[US] {{title}}"\n')


def project(tmp: Path, wiki_url: str, kinds: str = "", sub: str = "") -> Path:
    """Проект: адрес Confluence — подставной сервер, `confluence.space` — «T» (не пространство корня)."""
    if sub:
        tmp = tmp / sub
        tmp.mkdir()
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    text = cfg.read_text(encoding="utf-8").replace('    space: "T"\n', f'    space: "T"\n    base_url: "{wiki_url}"\n')
    cfg.write_text(text + kinds, encoding="utf-8")
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "US.md").write_text("шаблон", encoding="utf-8")
    return root


def doc(head: str, body: str = "# Итог\n\nПлатёж проводится за пять дней.\n") -> str:
    return f"---\n{head}---\n\n{body}\n{MARK}\n\n## Допущения\n\n- срок взят из практики\n"


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def publish(root: Path, rel: str, *extra: str) -> tuple:
    """`ship:publish` → (процесс, исход из машинной строки `AURORA-PUBLISH` или JSON при `--json`)."""
    env = {**os.environ, "CONFLUENCE_PAT": "test-token-not-real", "CONFLUENCE_USER": "", "CONFLUENCE_PASSWORD": ""}
    cp = subprocess.run([sys.executable, str(SCRIPTS / "publish_doc.py"), rel, *extra], cwd=str(root), env=env,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if "--json" in extra:
        res = json.loads(cp.stdout.strip().splitlines()[-1]) if cp.stdout.strip() else {}
    else:
        line = next((ln for ln in cp.stdout.splitlines() if ln.startswith("AURORA-PUBLISH ")), "")
        res = json.loads(line[len("AURORA-PUBLISH "):]) if line else {}
    return cp, res


def root_page(wiki: Wiki) -> str:
    """Корень вида и чужие страницы рядом — дети корня приходят несколькими ответами."""
    rid = wiki.add("Истории", None, SPACE)
    for title in ("Посторонняя 1", "Посторонняя 2", "Посторонняя 3"):
        wiki.add(title, rid, SPACE)
    return rid


# ------------------------------------------------------------------ подстановки: путь и заголовок

@test
def test_publish_target_fills_the_path_and_the_title_from_the_header(tmp: Path):
    """`make_kinds.publish_target`: уровни через `>`, пробелы по краям обрезаны; `{поле?}` без значения выбрасывает уровень,
    обязательное без значения — `missing-field`; заголовок по шаблону, лишние пробелы схлопнуты; встроенные `{title}`,
    `{file}`, `{kind}`, `{project}` важнее полей шапки; перевод строки в значении — пробел; длиннее 255 — `too-long`."""
    MK = _mod("make_kinds")
    text = doc('type: us\nrelease: "2.1"\nepic: PRJ-7\ntitle: "поле шапки"\ntags: [a, b]\nnote: "две\n')
    ctx = MK.publish_context(text, "Artifacts/us/US-7.md", "us", "PRJ-A")
    assert ctx["title"] == "Итог" and ctx["file"] == "US-7" and ctx["kind"] == "us" and ctx["project"] == "PRJ-A", ctx
    assert ctx["release"] == "2.1" and "tags" not in ctx, "список шапки служит подстановкой"
    rec = {"publish_path": "  Релиз {release} >{epic?}> {project} — {kind}  ", "publish_title": "[US]  {title}  · {file}"}
    t = MK.publish_target(rec, ctx)
    assert t["levels"] == ["Релиз 2.1", "PRJ-7", "PRJ-A — us"] and t["title"] == "[US] Итог · US-7" and not t["problems"], t
    no_epic = {k: v for k, v in ctx.items() if k != "epic"}
    t = MK.publish_target(rec, no_epic)
    assert t["levels"] == ["Релиз 2.1", "PRJ-A — us"] and not t["problems"], f"необязательный уровень не выпал: {t}"
    t = MK.publish_target({"publish_path": "Релиз {release} > Эпик {epic?}"}, no_epic)
    assert t["levels"] == ["Релиз 2.1"], f"уровень с пустой необязательной подстановкой остался: {t}"
    t = MK.publish_target(rec, {k: v for k, v in ctx.items() if k != "release"})
    assert t["missing"] == ["release"] and t["problems"][0] == ("missing-field", "release"), t
    # заголовок по умолчанию — {title}; необязательное в заголовке — пустая строка, пробелы схлопнуты
    assert MK.publish_target({}, ctx)["title"] == "Итог"
    assert MK.publish_target({"publish_title": "{epic?} {title}"}, no_epic)["title"] == "Итог"
    assert MK.publish_target({"publish_title": "{nothing?}"}, ctx)["problems"] == [("empty-title", "{nothing?}")]
    # значение без переводов строк; длинный заголовок — отказ
    assert MK.fill_slots("{a}", {"a": "две\nстроки\t  тут"})[0] == "две строки тут"
    long = MK.publish_target({"publish_title": "{title}"}, {**ctx, "title": "Я" * 300})
    assert ("too-long", "Я" * 60) in long["problems"], long["problems"]
    # без шапки и заголовка — имя файла; проект — из конфига
    bare = MK.publish_context("текст без заголовка", "Artifacts/us/Без-шапки.md")
    assert bare["title"] == "Без-шапки" == bare["file"], bare
    root = make_project(tmp)
    assert MK.project_name(str(root)) == "Test"
    # реестр видов знает новые поля и проверяет скобки
    assert {"publish_path", "publish_title"} <= set(MK.PUBLISH_FIELDS) <= set(MK.FIELDS)
    bad = MK.check(str(root), {"x": {"template": "", "out": "", "publish_path": "Релиз {release", "publish_title": "{} x"}})
    assert any("незакрытая фигурная скобка" in w for _k, w in bad) and any("пустая подстановка" in w for _k, w in bad), bad


# ------------------------------------------------------------------ цепочка разделов

@test
def test_chain_is_created_once_and_reused_and_the_space_comes_from_the_root(tmp: Path):
    """Первая публикация создаёт разделы «Релиз 1.0» › «PRJ-1» под корнем (в пространстве корня, а не конфига) и страницу
    «[US] Итог» под ними; вторая (другой документ того же релиза, без эпика) переиспользует «Релиз 1.0» и ложится прямо под
    него. Дети корня читаются постранично; разделы несут баннер Авроры и список дочерних страниц."""
    AC = _mod("aurora_common")
    wiki = Wiki()
    try:
        rid = root_page(wiki)
        root = project(tmp, wiki.url, KIND_US.format(root=f"{wiki.url}/pages/viewpage.action?pageId={rid}"))
        path1 = put(root, "Artifacts/us/US-1.md", doc("type: us\nstatus: ready\nrelease: \"1.0\"\nepic: PRJ-1\n"))
        cp, res = publish(root, "Artifacts/us/US-1.md", "--apply")
        assert cp.returncode == 0 and res["outcome"] == "ok", cp.stdout + cp.stderr
        assert wiki.children(rid)[-1] == "Релиз 1.0" and len(wiki.by_title("Релиз 1.0")) == 1, wiki.children(rid)
        rel_id = wiki.by_title("Релиз 1.0")[0]["id"]
        assert wiki.children(rel_id) == ["PRJ-1"], wiki.children(rel_id)
        epic_id = wiki.by_title("PRJ-1")[0]["id"]
        assert wiki.children(epic_id) == ["[US] Итог"], "страница документа легла не под последний уровень пути"
        posts = [b for m, _p, b in wiki.writes() if m == "POST"]
        assert [b["title"] for b in posts] == ["Релиз 1.0", "PRJ-1", "[US] Итог"], [b["title"] for b in posts]
        assert {b["space"]["key"] for b in posts} == {SPACE}, "пространство взято из конфига, а не у корня"
        section = wiki.by_title("Релиз 1.0")[0]["body"]
        assert "aurora-section" in section and 'ac:name="children"' in section and "aurora-generated" not in section, section
        assert res["created"] == ["Релиз 1.0", "PRJ-1"] and res["space"] == SPACE and res["root"]["from"] == "publish_url", res
        assert [c["state"] for c in res["chain"]] == ["created", "created"], res["chain"]
        fm = AC.frontmatter(path1.read_text(encoding="utf-8"))
        assert fm["published_space"] == SPACE and fm["published_parent"] == epic_id and len(fm["published_input"]) == 16, fm
        assert "publish_parent" not in fm, "публикация записала в шапку родителя — он затенил бы настройку вида"
        # чтение детей шло постранично: у корня четыре ребёнка, в ответе — по два
        assert any("start=2" in p for m, p, _b in wiki.log if m == "GET" and "/child/page" in p), "продолжение списка детей не читалось"

        put(root, "Artifacts/us/US-2.md", doc("type: us\nstatus: ready\nrelease: \"1.0\"\n", "# Второй\n\nТекст.\n"))
        before = len(wiki.writes())
        cp, res = publish(root, "Artifacts/us/US-2.md", "--apply")
        assert cp.returncode == 0 and res["outcome"] == "ok", cp.stdout + cp.stderr
        new = [b["title"] for m, _p, b in wiki.writes()[before:] if m == "POST"]
        assert new == ["[US] Второй"], f"раздел создан повторно или страница не создана: {new}"
        assert wiki.children(rel_id) == ["PRJ-1", "[US] Второй"], "без эпика страница легла не под релиз"
        assert [c["state"] for c in res["chain"]] == ["exists"] and res["created"] == [], res["chain"]
    finally:
        wiki.close()


@test
def test_dry_run_json_is_the_plan_and_writes_nothing(tmp: Path):
    """`ship:publish --json` без `--apply`: в stdout — один JSON плана (пространство, корень, цепочка с состоянием уровней,
    заголовок, действие); ни одного POST/PUT, файл не тронут. Корень можно дать адресом `/display/ПРОСТРАНСТВО/Заголовок`."""
    wiki = Wiki()
    try:
        rid = root_page(wiki)
        rel_id = wiki.add("Релиз 1.0", rid)
        root = project(tmp, wiki.url, KIND_US.format(root=f"{wiki.url}/display/{SPACE}/" + urllib.parse.quote("Истории")))
        path = put(root, "Artifacts/us/US-1.md", doc("type: us\nstatus: ready\nrelease: \"1.0\"\nepic: PRJ-9\n"))
        raw = path.read_bytes()
        cp, plan = publish(root, "Artifacts/us/US-1.md", "--json")
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert len(cp.stdout.strip().splitlines()) == 1, f"в stdout не один JSON: {cp.stdout[:300]}"
        assert plan["outcome"] == "ok" and plan["apply"] is False and plan["action"] == "create", plan
        assert plan["space"] == SPACE and plan["root"]["id"] == rid and plan["root"]["title"] == "Истории", plan["root"]
        assert [(c["title"], c["state"]) for c in plan["chain"]] == [("Релиз 1.0", "exists"), ("PRJ-9", "create")], plan["chain"]
        assert plan["chain"][0]["id"] == rel_id and plan["title"] == "[US] Итог" and plan["created"] == [], plan
        assert "Действие:" in cp.stderr and "AURORA-PUBLISH" not in cp.stdout, "вывод для человека попал в stdout"
        assert not wiki.writes() and path.read_bytes() == raw, "сухой прогон что-то записал"
        # без --json — та же строка машинно, для панели
        cp, res = publish(root, "Artifacts/us/US-1.md")
        assert res["outcome"] == "ok" and res["chain"] == plan["chain"] and "(dry-run)" in cp.stdout, cp.stdout
    finally:
        wiki.close()


@test
def test_a_missing_required_field_blocks_with_the_field_and_the_document_named(tmp: Path):
    """Обязательная подстановка без значения: отказ до любого обращения к серверу, причина называет поле и документ;
    серая кнопка в «Файлах» говорит то же человеческим языком и ведёт в настройки видов."""
    wiki = Wiki()
    try:
        rid = root_page(wiki)
        root = project(tmp, wiki.url, KIND_US.format(root=rid))
        path = put(root, "Artifacts/us/US-3.md", doc("type: us\nstatus: ready\nepic: PRJ-1\n"))
        raw = path.read_bytes()
        cp, res = publish(root, "Artifacts/us/US-3.md", "--apply")
        assert cp.returncode == 1 and res["outcome"] == "blocked" and res["code"] == "missing-field", cp.stdout + cp.stderr
        assert res["fields"] == ["release"] and "release" in res["why"] and "Artifacts/us/US-3.md" in res["why"], res
        assert not wiki.log, f"при пустой обязательной подстановке публикация обращалась к серверу: {wiki.log[:3]}"
        assert path.read_bytes() == raw
        state = _ck().file_read(str(root), "Artifacts/us/US-3.md")["publish"]
        assert state["state"] == "blocked" and state["fix"] == "kinds" and "release" in state["why"], state
        assert state["fields"] == ["release"], state
        # поле вписали — кнопка готова, путь посчитан сервером
        path.write_text(doc("type: us\nstatus: ready\nepic: PRJ-1\nrelease: \"3.0\"\n"), encoding="utf-8")
        state = _ck().file_read(str(root), "Artifacts/us/US-3.md")["publish"]
        assert state["state"] == "ready" and state["path"] == ["Релиз 3.0", "PRJ-1"] and state["title"] == "[US] Итог", state
    finally:
        wiki.close()


@test
def test_a_title_taken_elsewhere_in_the_space_blocks_and_moves_nothing(tmp: Path):
    """Заголовок уровня (или страницы) уже занят в пространстве страницей под другим родителем: отказ со ссылкой на неё,
    ни одной записи — чужие страницы публикация не двигает и вторую с тем же заголовком не заводит."""
    wiki = Wiki()
    try:
        rid = root_page(wiki)
        other = wiki.add("Где-то ещё", None)
        taken = wiki.add("Релиз 2.0", other)
        root = project(tmp, wiki.url, KIND_US.format(root=rid))
        put(root, "Artifacts/us/US-4.md", doc("type: us\nstatus: ready\nrelease: \"2.0\"\n"))
        cp, res = publish(root, "Artifacts/us/US-4.md", "--apply")
        assert cp.returncode == 1 and res["outcome"] == "blocked" and res["code"] == "title-taken", cp.stdout + cp.stderr
        assert res["url"].endswith(f"pageId={taken}") and res["chain"][0]["state"] == "refused", res
        assert not wiki.writes() and wiki.pages[taken]["parent"] == other, "чужую страницу тронули"
        # сухой прогон говорит то же самое, до отправки
        _cp, plan = publish(root, "Artifacts/us/US-4.md", "--json")
        assert plan["outcome"] == "blocked" and plan["code"] == "title-taken", plan
    finally:
        wiki.close()


@test
def test_explicit_parent_is_the_exact_place_and_the_kind_beats_the_header(tmp: Path):
    """Корень: `--parent` (точное место, путь вида не применяется) → `publish_url` вида → `publish_parent` шапки (наследие)
    → конфиг. Шапка больше не затеняет настройку вида — её туда и не пишут."""
    P = _mod("publish_doc")
    assert P.choose_parent("1", {"publish_parent": "2"}, {"publish_parent": "4"}, "3") == ("1", "--parent")
    assert P.choose_parent("", {"publish_parent": "2"}, {"publish_parent": "4"}, "3") == ("3", "publish_url вида артефакта")
    assert P.choose_parent("", {"publish_parent": "2"}, {"publish_parent": "4"}) == ("2", "publish_parent в шапке документа")
    assert P.choose_parent("", {}, {"publish_parent": "4"}) == ("4", "confluence.publish_parent в конфиге")
    wiki = Wiki()
    try:
        rid = root_page(wiki)
        exact = wiki.add("Сюда руками", None)
        root = project(tmp, wiki.url, KIND_US.format(root=rid))
        put(root, "Artifacts/us/US-5.md", doc(f"type: us\nstatus: ready\nrelease: \"1.0\"\npublish_parent: \"{exact}\"\n"))
        _cp, plan = publish(root, "Artifacts/us/US-5.md", "--json")
        assert plan["root"]["id"] == rid and plan["root"]["from"] == "publish_url", "шапка затенила вид"
        _cp, plan = publish(root, "Artifacts/us/US-5.md", "--json", "--parent", exact)
        assert plan["root"]["id"] == exact and plan["chain"] == [] and plan["root"]["from"] == "--parent", plan
    finally:
        wiki.close()
