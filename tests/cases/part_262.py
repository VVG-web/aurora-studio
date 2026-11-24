"""Проверки движка Aurora, часть 262: карточка жизненного цикла в «Файлах» (17-CARD).

Карточка читает модель (`ship_doc.lifecycle_model`) и запускает существующие
`ship:release`/`ship:export` через `/api/run`; второго пути выпуска нет (`/api/ship/run`
не существует). Публикация — отдельный необязательный бейдж вне линии из трёх узлов.
Здесь API, права и поведение страницы; схема, заморозка и `--verify` — в part_260.

Проекты — во временных папках, pandoc не запускается (записи и снимки собираются как
их пишет выпуск), сеть — только подставной сервер панели на 127.0.0.1.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from cases import part_260 as L
from harness import KIT, SCRIPTS, _js_function, make_project, panel_sources, set_home, test

BODY = "# История\n\nПлатёж проводится за пять дней.\n"
SRC = "Artifacts/us/Activity_A1/US-1.md"
DATE = "2026-10-19"
KINDS = ('\nartifacts:\n  us:\n    title: "История"\n    out: Artifacts/us\n'
         f'    publish_url: "https://wiki.example.com/pages/viewpage.action?pageId=777"\n'
         '  ac:\n    title: "Критерии"\n    out: Artifacts/ac\n')


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
    """Проект с видами `us` и `ac`; git есть — `created.at` тогда берётся из коммита."""
    root = make_project(tmp, git=True)
    cfg = root / "aurora.config.yaml"
    text = cfg.read_text(encoding="utf-8")
    # Адрес Confluence нужен, чтобы состояние кнопки было `ready`, а не `blocked`.
    text = text.replace('    space: "T"\n',
                        '    space: "T"\n    base_url: "https://wiki.example.com"\n')
    cfg.write_text(text + KINDS, encoding="utf-8")
    return root


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _put(root: Path, rel: str, data) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _commit(root: Path, message: str = "fixture") -> None:
    L._git(root, "add", "-A")
    L._git(root, "commit", "-qm", message)


def _source(root: Path, rel: str = SRC, head: str = "", body: str = BODY) -> str:
    text = ('---\ntitle: "US-1"\ndoc: US-1\nversion: "1.0"\ntype: us\nstatus: ready\n'
            'artifact_id: ART-0001\nbased_on: ["[[REQ-001]]"]\n' + head + '---\n\n') + body
    _put(root, rel, text)
    return text


def _snapshot(rel: str, version: str, date: str, source_text: str, *, kind: str = "us",
              doc: str = "US-1", extra: str = "", body: str = BODY) -> str:
    """Снимок в том виде, в каком его пишет заморозка: шапка с `source` и `sha256` тела."""
    C = _mod("aurora_common")
    head = ('title: "' + doc + '"\ndoc: ' + doc + '\nversion: ' + version +
            '\ntype: deliverable\nkind: ' + kind + '\nfrozen_at: ' + date + 'T00:00:00Z\n'
            'source: ' + rel + '\nsource_sha256: ' + hashlib.sha256(source_text.encode("utf-8")).hexdigest() +
            '\nsource_id: ART-0001\nsource_clean_sha: ' + C.clean_sha(source_text) + '\n' + extra)
    base = "---\n" + head + "---\n\n" + body
    return "---\n" + head + "sha256: " + C.release_hash(base) + "\n---\n\n" + body


def _snapshot_rel(version: str = "1.0", date: str = DATE, src: str = SRC) -> str:
    return f"Deliverables/us/{Path(src).stem}_v{version}_{date}.md"


def _freeze(root: Path, *, version: str = "1.0", date: str = DATE, src: str = SRC,
            write_snapshot: bool = True) -> dict:
    """Снимок на диске плюс запись `frozen[]` в исходнике — доказательство как у выпуска."""
    C = _mod("aurora_common")
    text = (root / src).read_text(encoding="utf-8")
    snap_rel = _snapshot_rel(version, date, src)
    snap = _snapshot(src, version, date, text)
    if write_snapshot:
        _put(root, snap_rel, snap)
    rec = {"version": version, "at": date + "T00:00:00Z", "path": snap_rel,
           "sha256": C.release_hash(snap), "kind": "us", "doc": "US-1", "source": src,
           "source_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
           "source_id": "ART-0001", "source_clean_sha": C.clean_sha(text), "legacy": False}
    _put(root, src, C.with_records(text, "frozen", [rec]))
    return rec


def _release(root: Path, *, version: str = "1.0", date: str = DATE, src: str = SRC,
             frozen: dict | None = None, write_docx: bool = True) -> dict:
    """docx на диске плюс запись `released_versions[]`; связь с заморозкой — `frozen_*`."""
    C = _mod("aurora_common")
    frozen = frozen or _freeze(root, version=version, date=date, src=src)
    text = (root / src).read_text(encoding="utf-8")
    docx_rel = os.path.splitext(frozen["path"])[0] + ".docx"
    data = b"PK\x03\x04 docx " + BODY.encode("utf-8")
    if write_docx:
        _put(root, docx_rel, data)
    rec = {"version": version, "at": date + "T00:00:00Z", "path": docx_rel,
           "sha256": hashlib.sha256(data).hexdigest(), "kind": "us", "doc": "US-1",
           "source": src, "frozen_path": frozen["path"], "frozen_sha256": frozen["sha256"],
           "format": "docx", "legacy": False}
    _put(root, src, C.with_records(text, "released_versions", [rec]))
    return rec


# ------------------------------------------------------------------ модель: что читает карточка

@test
def test_lifecycle_model_created_published_frozen_released(tmp: Path):
    """Модель, стадии, `required_for`, `current`, `publication`: карточка не угадывает стадию.

    Публикация — отдельное поле и необязательный шаг: её наличие не двигает стадию и не
    входит в `required_for`. Заморозка и выпуск — записи исходника плюс файлы на диске.
    """
    S, C = _mod("ship_doc"), _mod("aurora_common")
    root = _project(tmp)
    _source(root)
    _commit(root, "art")
    m = S.lifecycle_model(str(root), SRC)
    assert m["applicable"] is True and m["path"] == SRC and m["kind"] == "us", m
    assert m["stage"] == "created" and m["current"] == "frozen", m
    assert m["required_for"] == {"freeze": ["created"], "release": ["created", "frozen"]}, m
    assert m["optional"] == ["published"] and m["frozen"] == [] and m["released_versions"] == [], m
    assert m["created"]["exists"] is True and m["created"]["at"], m["created"]
    assert m["publication"]["state"] == "none" and m["publication"]["done"] is False, m["publication"]
    assert m["problems"] == [] and m["warnings"] == [], m

    # Публикация: бейдж, а не стадия. Штамп не меняет отпечаток чистовика.
    text = (root / SRC).read_text(encoding="utf-8")
    text = text.replace("---\n\n", 'confluence_page_id: "884422"\npublished: 2026-10-20\n'
                        'published_url: https://wiki.example.com/pages/viewpage.action?pageId=884422\n'
                        f'published_sha: {C.clean_sha(text)}\n---\n\n', 1)
    _put(root, SRC, text)
    _commit(root, "published")
    m = S.lifecycle_model(str(root), SRC)
    assert m["stage"] == "created" and m["current"] == "frozen", m
    assert m["publication"]["state"] == "published" and m["publication"]["done"] is True, m["publication"]
    assert m["publication"]["stale"] is False and m["publication"]["url"].endswith("pageId=884422"), m["publication"]
    assert "published" not in m["required_for"]["freeze"] + m["required_for"]["release"], m["required_for"]

    frozen = _freeze(root)
    _commit(root, "freeze")
    m = S.lifecycle_model(str(root), SRC)
    assert m["stage"] == "frozen" and m["current"] == "released", m
    assert len(m["frozen"]) == 1 and m["frozen"][0]["ok"] is True, m["frozen"]
    assert m["frozen"][0]["path"] == frozen["path"] and m["frozen"][0]["version"] == "1.0", m["frozen"]
    assert m["released_versions"] == [] and m["next_version"] == "1.1", m

    _release(root, frozen=frozen)
    _commit(root, "release")
    m = S.lifecycle_model(str(root), SRC)
    assert m["stage"] == "released" and m["current"] == "", m
    assert len(m["released_versions"]) == 1 and m["released_versions"][0]["ok"] is True, m["released_versions"]
    assert m["released_versions"][0]["legacy"] is False, m["released_versions"][0]
    assert m["released_versions"][0]["frozen_path"] == frozen["path"], m["released_versions"][0]
    assert m["publication"]["state"] == "published", "выпуск стёр публикацию: бейдж отдельный"


@test
def test_lifecycle_current_is_next_step(tmp: Path):
    """`current` — следующий обязательный шаг, а не `stage`: created → frozen, frozen → released, released → пусто."""
    S = _mod("ship_doc")
    root = _project(tmp)
    _source(root)
    _commit(root, "art")
    got = []
    m = S.lifecycle_model(str(root), SRC)
    got.append((m["stage"], m["current"]))
    frozen = _freeze(root)
    _commit(root, "freeze")
    m = S.lifecycle_model(str(root), SRC)
    got.append((m["stage"], m["current"]))
    _release(root, frozen=frozen)
    _commit(root, "release")
    m = S.lifecycle_model(str(root), SRC)
    got.append((m["stage"], m["current"]))
    assert got == [("created", "frozen"), ("frozen", "released"), ("released", "")], got
    assert S.lifecycle_model(str(root), SRC)["next_version"] == "1.1", "после выпуска следующая версия не подросла"


@test
def test_lifecycle_model_warnings_and_problems(tmp: Path):
    """Предупреждения и проблемы: правка после заморозки, удалённый снимок, удалённый docx, снимок без источника.

    Карточка показывает их причиной (`code` и `detail`), а не только цветом узла.
    """
    S, C = _mod("ship_doc"), _mod("aurora_common")
    root = _project(tmp)
    _source(root)
    _commit(root, "art")
    frozen = _freeze(root)
    _commit(root, "freeze")
    # Исходник изменился после заморозки: предупреждение, стадия остаётся frozen.
    _put(root, SRC, (root / SRC).read_text(encoding="utf-8").replace("пять дней", "семь дней"))
    _commit(root, "source changed")
    m = S.lifecycle_model(str(root), SRC)
    assert m["changes_after_freeze"] is True, m
    assert "source-changed-after-freeze" in m["warnings"], m["warnings"]
    assert m["stage"] == "frozen" and m["problems"] == [], m

    # Снимок удалён: доказательство пропало, узел заморозки не `ok`.
    (root / frozen["path"]).unlink()
    _commit(root, "snapshot gone")
    m = S.lifecycle_model(str(root), SRC)
    codes = {p["code"] for p in m["problems"]}
    assert "snapshot-missing" in codes and m["frozen"][0]["ok"] is False, m
    assert m["stage"] == "frozen", "без снимка стадия не должна откатываться в created"

    # docx удалён: запись выпуска есть, файла нет.
    root2 = make_project(_mkdir(tmp / "second"), git=True)
    cfg = root2 / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + KINDS, encoding="utf-8")
    _source(root2)
    _commit(root2, "art")
    released = _release(root2)
    _commit(root2, "release")
    (root2 / released["path"]).unlink()
    _commit(root2, "docx gone")
    m = S.lifecycle_model(str(root2), SRC)
    codes = {p["code"] for p in m["problems"]}
    assert "release-missing" in codes and m["released_versions"][0]["ok"] is False, m
    assert m["stage"] == "released", "запись выпуска осталась: стадия не откатывается"

    # Снимок без источника: шапка не называет исходник.
    _put(root, "Deliverables/us/без_источника_v1.0_2026-01-01.md",
         "---\ntitle: \"Без источника\"\ndoc: Без источника\nversion: \"1.0\"\n"
         "type: deliverable\nkind: us\nfrozen_at: 2026-01-01T00:00:00Z\n---\n\n# Текст\n")
    _commit(root, "orphan")
    m = S.lifecycle_model(str(root), "Deliverables/us/без_источника_v1.0_2026-01-01.md")
    assert m["applicable"] is True and m["current"] == "frozen", m
    assert any(p["code"] == "source-missing" for p in m["problems"]), m["problems"]


# ------------------------------------------------------------------ применимость карточки

@test
def test_lifecycle_applicable_not_every_file(_t):
    """Карточка открывается исходникам и доказательствам, а не каждому `.md`.

    Регистр папки и вида не важен; `README.md` — проводник, `work2` — не вид, чужое
    расширение карточку не открывает.
    """
    ck = _ck()
    kinds = {"us": {}, "ac": {}}
    yes = [
        "Artifacts/us/US-1.md",
        "Deliverables/work/ОПЗ_v1.md",
        "Deliverables/work/sub/ОПЗ_v1.md",
        "Deliverables/released/ОПЗ_v1.md",
        "Deliverables/_archive/ОПЗ_v1.md",
        "Deliverables/released/ОПЗ_v1.docx",
        "Deliverables/us/US-1.md",
        "Deliverables/us/sub/US-1.md",
        "Deliverables/deliverable/ОПЗ_v1.md",
        "deliverables/us/US-1.md",
        "artifacts/us/US-1.md",
        "Deliverables/us/US-1.DOCX",
    ]
    no = [
        "Artifacts/us/README.md",
        "Artifacts/work2/x.md",
        "Artifacts/us/x.docx",
        "Deliverables/released/old.pdf",
        "Deliverables/us/old.pdf",
        "Deliverables/work2/x.md",
        "Deliverables/README.md",
        "Deliverables/us/README.md",
        "README.md",
        "docs/lifecycle.md",
        "Raw/customer/договор.md",
        "Sources/Confluence/страница.md",
        "Artifacts/README.md",
    ]
    for rel in yes:
        assert ck.lifecycle_applicable(rel, kinds) is True, f"карточка не открывается: {rel}"
    for rel in no:
        assert ck.lifecycle_applicable(rel, kinds) is False, f"карточка открывается лишнему: {rel}"
    # Чистая функция пути и видов: файла нет, содержимое не читается.
    assert ck.lifecycle_applicable("Deliverables/us/которого_нет.md", kinds) is True


@test
def test_no_auto_card_for_every_text_file(tmp: Path):
    """`file_tree` ставит `applicable` только документам видов, а не каждому текстовому файлу."""
    root = _project(tmp)
    _source(root)
    _put(root, "Artifacts/us/README.md", "# Проводник папки\n")
    _put(root, "Deliverables/work/ОПЗ_v1.md", _source(root).replace('type: us', 'type: deliverable'))
    _put(root, "Deliverables/work2/x.md", "# Мимо\n")
    _put(root, "Deliverables/README.md", "# Проводник\n")
    _put(root, "docs/lifecycle.md", "# Документ кита\n")
    ck = _ck()
    rows = {r["path"]: r for r in ck.file_tree(str(root))["files"]}
    assert rows[SRC]["applicable"] is True, rows[SRC]
    assert rows["Deliverables/work/ОПЗ_v1.md"]["applicable"] is True, rows["Deliverables/work/ОПЗ_v1.md"]
    for rel in ("Artifacts/us/README.md", "Deliverables/work2/x.md", "Deliverables/README.md", "docs/lifecycle.md"):
        assert rows[rel]["applicable"] is False, f"карточка у каждого текстового файла: {rel}"
    # Виды читаются один раз на дерево, без чтения содержимого ради `applicable`.
    assert "AG_KINDS.read_kinds" in (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ права и публикация

@test
def test_publish_info_none_for_snapshot_and_docx(tmp: Path):
    """Публикация снимка и docx запрещена: это доказательство, а не исходник. `Deliverables/work` остаётся публикуемым."""
    ck = _ck()
    root = _project(tmp)
    _source(root)
    _put(root, "Deliverables/work/ОПЗ_v1.md",
         '---\ntitle: "ОПЗ"\ndoc: ОПЗ\nversion: "1.0"\ntype: deliverable\nstatus: ready\n---\n\n# ОПЗ\n')
    _put(root, "Deliverables/released/ОПЗ_v1.md", "---\nsource: Deliverables/work/ОПЗ_v1.md\n---\n\n# ОПЗ\n")
    _put(root, "Deliverables/us/US-1_v1.0_2026-10-19.md", "# Снимок\n")
    _put(root, "Deliverables/us/US-1_v1.0_2026-10-19.docx", b"PK\x03\x04 docx")
    _put(root, "Artifacts/us/US-1.docx", b"PK\x03\x04 docx")

    def state(rel: str) -> str:
        d = ck.file_read(str(root), rel)
        if d.get("binary"):
            assert "publish" not in d, "у docx появилась кнопка публикации"
            return "none"
        assert "publish" in d, (rel, d)
        return d["publish"]["state"]

    assert state(SRC) == "ready", "исходник вида публикуется как раньше"
    assert state("Deliverables/work/ОПЗ_v1.md") in ("ready", "blocked"), "Deliverables/work перестал публиковаться"
    for rel in ("Deliverables/released/ОПЗ_v1.md", "Deliverables/us/US-1_v1.0_2026-10-19.md",
                "Deliverables/us/US-1_v1.0_2026-10-19.docx", "Artifacts/us/US-1.docx"):
        assert state(rel) == "none", f"публикация снимка или docx не закрыта: {rel}"
    # Причина у снимка не «не markdown», а доказательство: порядок проверок важен.
    assert ck.publish_info(str(root), "Deliverables/us/x.md", "", {}, "") == {"state": "none"}


@test
def test_readonly_blocks_edit_delete_and_symlink(tmp: Path):
    """`<вид>`, `released`, `_archive` закрыты для правки и удаления; `work` правится; ссылка не обходит запрет."""
    ck = _ck()
    root = _project(tmp)
    _source(root)
    _put(root, "Deliverables/work/ОПЗ_v1.md", "---\ntitle: ОПЗ\n---\n\n# ОПЗ\n")
    _put(root, "Deliverables/us/ОПЗ_v1.md", "---\nsource: Deliverables/work/ОПЗ_v1.md\n---\n\n# снимок\n")
    _put(root, "Deliverables/released/ОПЗ_v1.md", "---\nsource: Deliverables/work/ОПЗ_v1.md\n---\n\n# выпуск\n")
    reasons = {
        "Deliverables/us/ОПЗ_v1.md": "снимок",
        "Deliverables/us/sub/ОПЗ_v1.md": "снимок",
        "Deliverables/released/ОПЗ_v1.md": "снимок",
        "Deliverables/_archive/ОПЗ_v1.md": "снимок",
        "DELIVERABLES/US/ОПЗ_v1.md": "снимок",
    }
    for rel, word in reasons.items():
        why = ck.why_readonly(rel)
        assert word in why and "доказательство" in why, f"нет причины запрета: {rel}: {why!r}"
    assert ck.why_readonly("Deliverables/work/ОПЗ_v1.md") == "", "Deliverables/work закрыт для правки"
    assert ck.why_readonly("Deliverables/work/sub/ОПЗ_v1.md") == "", "вложенная папка work закрыта"

    # Запись и удаление снимка отказывают, work пишется.
    got = ck.file_write(str(root), "Deliverables/released/ОПЗ_v1.md", "# правка\n")
    assert got.get("error") and "только для чтения" in got["error"], got
    assert (root / "Deliverables/released/ОПЗ_v1.md").read_text(encoding="utf-8").startswith("---"), "снимок перезаписан"
    assert ck.file_delete(str(root), "Deliverables/us/ОПЗ_v1.md").get("error"), "снимок удалён через панель"
    assert (root / "Deliverables/us/ОПЗ_v1.md").is_file(), "файл снимка исчез после отказа"
    assert ck.file_write(str(root), "Deliverables/work/ОПЗ_v1.md", "---\ntitle: ОПЗ\n---\n\n# ОПЗ правленый\n").get("ok"), \
        "Deliverables/work больше не правится"

    # Ссылка с обычным именем на снимок закрыта так же, как сам снимок.
    if hasattr(os, "symlink"):
        link = root / "Artifacts/us/ссылка.md"
        try:
            os.symlink(root / "Deliverables/us/ОПЗ_v1.md", link)
        except (OSError, NotImplementedError):
            return
        assert ck.why_readonly_real(str(root), "Artifacts/us/ссылка.md", str(link)) != "", \
            "why_readonly_real не видит ссылку на снимок"
        row = next((r for r in ck.file_tree(str(root))["files"] if r["path"] == "Artifacts/us/ссылка.md"), None)
        assert row and row["readonly"], "file_tree показывает ссылку на снимок правимой"
        assert ck.file_write(str(root), "Artifacts/us/ссылка.md", "# правка\n").get("error"), \
            "запись по ссылке на снимок прошла"


# ------------------------------------------------------------------ HTTP: карточка, план, копия

class _Panel:
    """Настоящий сервер панели на свободном порту; проекты — временные папки."""

    def __init__(self, roots):
        sys.path.insert(0, str(KIT / "cockpit"))
        sys.path.insert(0, str(SCRIPTS))
        self.ck = importlib.import_module("aurora_cockpit")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.ck.Handler)
        self.srv.roots = [str(r) for r in roots]
        self.ck.SCHED = self.ck.CRON.Scheduler(self.ck, lambda: self.ck.find_projects(self.srv.roots))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method: str, path: str, payload=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=120)
        hdrs = {"X-Aurora-Token": self.ck.TOKEN}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        c.request(method, path, body=body, headers=hdrs)
        r = c.getresponse()
        raw = r.read()
        head = {k.lower(): v for k, v in r.getheaders()}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = raw
        return r.status, head, data

    def get(self, path: str, **kw):
        return self.req("GET", path, **kw)

    def post(self, path: str, payload, **kw):
        return self.req("POST", path, payload, **kw)

    def close(self):
        self.srv.shutdown()
        self.ck.SCHED = None


def _q(project: Path, rel: str) -> str:
    return ("?project=" + quote(str(project), safe="") + "&path=" + quote(rel, safe=""))


@test
def test_lifecycle_api_rejects_bad_path_secret_symlink(tmp: Path):
    """`/api/files/lifecycle`: только внутри проекта и не служебное; `..`, секрет и ссылка наружу — отказ."""
    set_home(tmp / "home")
    root = _project(tmp)
    _source(root)
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("---\ntype: us\n---\n\n# чужое\n", encoding="utf-8")
    _put(root, ".env", "TOKEN=очень-секретный\n")
    if hasattr(os, "symlink"):
        try:
            os.symlink(outside / "secret.md", root / "Artifacts/us/наружу.md")
        except (OSError, NotImplementedError):
            pass
    panel = _Panel([tmp])
    try:
        status, _head, data = panel.get("/api/files/lifecycle" + _q(root, SRC))
        assert status == 200 and data["applicable"] is True and data["stage"] == "created", (status, data)
        for rel in ("../outside/secret.md", "Artifacts/../../outside/secret.md", "/etc/passwd"):
            status, _head, data = panel.get("/api/files/lifecycle" + _q(root, rel))
            assert status == 404 and data.get("code") == "outside", (rel, status, data)
        status, _head, data = panel.get("/api/files/lifecycle" + _q(root, ".env"))
        assert status == 403 and data.get("code") == "service", (status, data)
        assert "секрет" not in json.dumps(data, ensure_ascii=False), data
        if (root / "Artifacts/us/наружу.md").is_symlink():
            status, _head, data = panel.get("/api/files/lifecycle" + _q(root, "Artifacts/us/наружу.md"))
            assert status == 404 and data.get("code") == "outside", (status, data)
        # Удалённый токен карточку не получает: 403 на границе HTTP, до обработчика.
        real = panel.ck.G.authenticate
        panel.ck.G.authenticate = lambda *a, **kw: panel.ck.G.AuthContext(
            kind="remote", token_id="t", projects=(str(root),))
        try:
            for path in ("/api/files/lifecycle" + _q(root, SRC), "/api/ship/plan", "/api/files/download" + _q(root, SRC)):
                method = "POST" if path == "/api/ship/plan" else "GET"
                status, _head, data = panel.req(method, path, {"project": str(root), "path": SRC}
                                                if method == "POST" else None)
                assert status == 403 and data.get("code") == "endpoint", (path, status, data)
        finally:
            panel.ck.G.authenticate = real
    finally:
        panel.close()


@test
def test_ship_plan_writes_nothing(tmp: Path):
    """`/api/ship/plan` — чтение: `freeze -> export -> release` в ответе, дерево и git не тронуты."""
    set_home(tmp / "home")
    root = _project(tmp)
    _source(root)
    _commit(root, "art")
    before, status_git = L._tree_digest(root), L._git_status(root)
    panel = _Panel([tmp])
    try:
        for action in ("both", "freeze", "release"):
            status, _head, plan = panel.post("/api/ship/plan", {"project": str(root), "path": SRC, "action": action})
            assert status == 200 and plan["ok"] is True and plan["action"] == action, (action, status, plan)
            assert plan["problems"] == [] and plan["steps"], plan
            acts = [s.get("action") for s in plan["steps"]]
            if action == "freeze":
                assert acts == ["freeze"], plan["steps"]
            else:
                assert acts == ["freeze", "export", "release"], f"«Выпустить» из created не freeze -> export -> release: {plan}"
                assert plan["steps"][0]["path"].startswith("Deliverables/us/"), plan["steps"]
                assert plan["steps"][0]["path"].endswith(".md"), "заморозка пишет снимок, а не docx"
                assert plan["steps"][1]["path"].endswith(".docx"), plan["steps"]
            assert plan.get("version") == "1.0", plan
        status, _head, bad = panel.post("/api/ship/plan", {"project": str(root), "path": SRC, "action": "deliver"})
        assert status == 400 and bad["ok"] is False and bad["code"] == "bad-action", (status, bad)
        status, _head, bad = panel.post("/api/ship/plan", {"project": str(root), "path": "../outside/secret.md", "action": "both"})
        assert status == 404 and bad["code"] == "outside", (status, bad)
    finally:
        panel.close()
    assert L._tree_digest(root) == before, "план выпуска что-то записал"
    assert L._git_status(root) == status_git, "план выпуска тронул git"
    assert not (root / ".aurora" / "state" / "lifecycle").exists(), "план выпуска поставил замок"
    assert not list((root / "Deliverables").rglob("*.docx")), "план выпуска сделал docx"


@test
def test_ship_download_only_local(tmp: Path):
    """`/api/files/download`: байты только локальной сессии, типы docx/md/прочее, имя без кавычек и переводов строк."""
    set_home(tmp / "home")
    root = _project(tmp)
    _source(root)
    docx = b"PK\x03\x04 docx"
    _put(root, "Deliverables/us/US-1_v1.0_2026-10-19.docx", docx)
    _put(root, "Deliverables/us/схема.pdf", b"%PDF-1.4")
    _put(root, ".env", "TOKEN=секрет\n")
    panel = _Panel([tmp])
    try:
        status, head, data = panel.get("/api/files/download" + _q(root, "Deliverables/us/US-1_v1.0_2026-10-19.docx"))
        assert status == 200 and data == docx, (status, data[:40])
        assert head["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document", head
        assert head["content-disposition"] == 'attachment; filename="US-1_v1.0_2026-10-19.docx"', head
        assert head.get("x-content-type-options") == "nosniff", head
        status, head, data = panel.get("/api/files/download" + _q(root, SRC))
        assert status == 200 and head["content-type"].startswith("text/markdown") and "Платёж".encode("utf-8") in data, head
        status, head, data = panel.get("/api/files/download" + _q(root, "Deliverables/us/схема.pdf"))
        assert status == 200 and head["content-type"] == "application/octet-stream" and data == b"%PDF-1.4", head
        for rel in (".env", "../outside/secret.md", "Artifacts/../../outside/secret.md"):
            status, _head, data = panel.get("/api/files/download" + _q(root, rel))
            assert status in (403, 404), (rel, status, data)
        if os.name != "nt":
            _put(root, 'Deliverables/us/имя"с\nпереводом.md', "# текст\n")
            status, head, data = panel.get("/api/files/download" + _q(root, 'Deliverables/us/имя"с\nпереводом.md'))
            disposition = head["content-disposition"]
            assert status == 200 and "\n" not in disposition and "\r" not in disposition, disposition
            # Кавычки в имени — только обрамление `filename="…"`, внутри их нет.
            assert disposition.count('"') == 2, f"кавычка из имени попала в заголовок: {disposition}"
        # Больше 50 МБ — отказ, а не чтение в память.
        big = root / "Deliverables/us/большой.md"
        with open(big, "wb") as f:
            f.truncate(panel.ck.DOWNLOAD_LIMIT + 1)
        status, _head, data = panel.get("/api/files/download" + _q(root, "Deliverables/us/большой.md"))
        assert status == 413 and data.get("error"), (status, data)
        # Удалённому токену копия не отдаётся.
        real = panel.ck.G.authenticate
        panel.ck.G.authenticate = lambda *a, **kw: panel.ck.G.AuthContext(
            kind="remote", token_id="t", projects=(str(root),))
        try:
            status, _head, data = panel.get("/api/files/download" + _q(root, SRC))
            assert status == 403 and data.get("code") == "endpoint", (status, data)
            assert "Платёж" not in json.dumps(data, ensure_ascii=False)
        finally:
            panel.ck.G.authenticate = real
    finally:
        panel.close()


@test
def test_no_ship_run_route(tmp: Path):
    """Второго пути выпуска нет: `/api/ship/run` не существует ни в роутере, ни в списках прав."""
    panel = _Panel([tmp])
    try:
        src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
        assert "/api/ship/run" not in src, "появился второй путь выпуска"
        assert "/api/ship/plan" in src and "/api/files/lifecycle" in src and "/api/files/download" in src
        guard = (KIT / "scripts/aurora_guard.py").read_text(encoding="utf-8")
        for path in ("/api/files/lifecycle", "/api/ship/plan", "/api/files/download"):
            assert path not in guard, f"удалённый токен получает {path}: путь в списках прав"
        status, _head, data = panel.post("/api/ship/run", {"project": str(tmp), "path": SRC})
        assert status == 404, (status, data)
        # Роутер: новый POST назван поимённо, а не проваливается в общий отказ.
        post = src[src.index("    def do_POST(self):"):]
        assert 'u.path == "/api/ship/plan"' in post, "POST-план не назван в роутере"
    finally:
        panel.close()


# ------------------------------------------------------------------ карточка на настоящем коде страницы
#
# Разметку строит `drawLifecycle` из `el(...)`, а не из трёх копий в HTML. Здесь она
# исполняется настоящим кодом `panel.js` в node; DOM — минимальная заготовка: узлы,
# классы, атрибуты, события и таймеры. Сеть и модель не нужны.

DOM_STUBS = r"""
const CALLS = [], POLLS = [], TOASTS = [], SHOWN = [], REVEALS = [], FOCUSED = [];
let CONFIRM = true;
let REPLY = () => ({});
let TIMERS = [];

class Node {
  constructor(tag){
    this.tag = tag; this.attrs = {}; this.kids = []; this.dataset = {}; this.style = {};
    this.listeners = {}; this.hidden = false; this.disabled = false; this.parent = null;
    this.nodeType = tag === "#text" ? 3 : 1; this.text = ""; this.value = ""; this.open = false;
    this.textContent = ""; this.title = "";
    this._classes = new Set();
    this.classList = {
      add: c => this._classes.add(c), remove: c => this._classes.delete(c),
      contains: c => this._classes.has(c),
      toggle: c => this._classes.has(c) ? this._classes.delete(c) : this._classes.add(c),
    };
  }
  get className(){ return [...this._classes].join(" "); }
  set className(v){ this._classes = new Set(String(v || "").split(/\s+/).filter(Boolean)); }
  setAttribute(k, v){
    if (k === "class"){ this.className = v; return; }
    this.attrs[k] = String(v);
    if (k === "id") IDS[String(v)] = this;
    if (k.startsWith("data-")) this.dataset[k.slice(5)] = String(v);
    if (k === "hidden") this.hidden = true;
    if (k === "disabled") this.disabled = true;
  }
  getAttribute(k){ return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener(t, fn){ (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, ev){ (this.listeners[t] || []).forEach(fn => fn(ev || {})); }
  append(...kids){
    for (const kid of kids){
      if (kid === null || kid === undefined) continue;
      let node = kid;
      if (!(kid instanceof Node)){ node = new Node("#text"); node.text = String(kid); }
      node.parent = this; this.kids.push(node);
    }
  }
  prepend(...kids){
    const add = kids.filter(k => k !== null && k !== undefined);
    add.forEach(k => { if (k instanceof Node) k.parent = this; });
    this.kids = [...add, ...this.kids];
  }
  remove(){ if (this.parent) this.parent.kids = this.parent.kids.filter(k => k !== this); }
  replaceChildren(...kids){ this.kids = []; this.append(...kids); }
  set innerHTML(v){ if (v === "") this.kids = []; }
  get innerHTML(){ return ""; }
  focus(){ document.activeElement = this; this.dispatch("focus"); FOCUSED.push(this); }
  blur(){ this.dispatch("blur"); }
  getBoundingClientRect(){ return {top: 10, left: 10, bottom: 60, right: 120, width: 110, height: 50}; }
  querySelector(sel){ return pickOne(sel, this); }
  querySelectorAll(sel){ return pickAll(sel, this); }
}
const IDS = {};
const document = {
  createElement: tag => new Node(tag),
  createTextNode: text => { const n = new Node("#text"); n.text = String(text); return n; },
  body: new Node("body"),
  activeElement: null,
  querySelector: sel => pickOne(sel, document.body),
  getElementById: id => IDS[id] || null,
};
const innerWidth = 200, innerHeight = 768;
const text = n => n === null || n === undefined ? "" : (n.tag === "#text" ? n.text : (n.kids || []).map(text).join(" "));
const _match = (node, token) => {
  let rest = token;
  const attr = /\[([\w-]+)(?:="([^"]*)")?\]/.exec(rest);
  if (attr){
    const have = node.getAttribute(attr[1]);
    if (attr[2] === undefined ? have === null : String(have) !== attr[2]) return false;
    rest = rest.replace(attr[0], "");
  }
  const classes = [...rest.matchAll(/\.([\w-]+)/g)].map(m => m[1]);
  if (classes.some(c => !node._classes.has(c))) return false;
  rest = rest.replace(/\.[\w-]+/g, "").trim();
  return !rest || node.tag === rest;
};
const _walk = (node, fn) => { for (const kid of node.kids || []){ fn(kid); _walk(kid, fn); } };
const pickAll = (sel, root) => {
  const parts = String(sel).trim().split(/\s+/).filter(Boolean);
  let nodes = [root];
  for (const part of parts){
    const next = [];
    for (const base of nodes) _walk(base, n => { if (_match(n, part) && !next.includes(n)) next.push(n); });
    nodes = next;
  }
  return nodes;
};
const pickOne = (sel, root) => pickAll(sel, root)[0] || null;
const $ = (sel, root) => {
  sel = String(sel).trim();
  const deep = /^#([\w-]+)\s+(.+)$/.exec(sel);
  if (deep){ const base = IDS[deep[1]]; return base ? pickOne(deep[2], base) : null; }
  if (sel.startsWith("#")) return IDS[sel.slice(1)] || null;
  return pickOne(sel, root || document.body);};
const el = (tag, attrs = {}, ...kids) => {
  const n = new Node(tag);
  for (const [k, v] of Object.entries(attrs || {})){
    if (v === null || v === undefined) continue;
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) n.append(kid);
  return n;
};
const t = (k, v) => String(k) + (v ? JSON.stringify(v) : "");
const S = {project: {path: "/p"}, lang: "ru", view: "files"};
const TOKEN = "tok";
let F = {path: "", files: [], lifecycle: null, lifecyclePath: "", lifecyclePick: "",
         lifecycleBusy: false, dirty: false, ed: null, publishable: true, createKind: "", reopen: ""};
const show = v => SHOWN.push(v);
const toast = (m, k) => TOASTS.push([m, k]);
const api = async (path, opts = {}) => { CALLS.push([path, opts]); return REPLY(path, opts); };
const poll = (id, since, label) => { POLLS.push([id, since, label]); };
const confirm = () => CONFIRM;
const revealFile = (...args) => REVEALS.push(args);
const destroyEditor = () => { F.ed = null; };
const saveFile = async () => {};
const drawTree = () => {};
const setTimeout = (fn, ms) => { TIMERS.push({fn, ms}); return TIMERS.length; };
const clearTimeout = id => { if (id && TIMERS[id - 1]) TIMERS[id - 1].fn = null; };
const runTimers = () => { const list = TIMERS.slice(); TIMERS = []; list.forEach(x => x.fn && x.fn()); };
"""


def _ui(*heads: str) -> str:
    ui = panel_sources()
    return "".join(_js_function(ui, head) + "\n}\n" for head in heads)


def _node(code: str, tmp: Path) -> dict:
    node = shutil.which("node")
    assert node, "нет node: поведение карточки проверить нечем — поставьте его, иначе регрессия доедет до человека"
    src = tmp / "card.mjs"
    src.write_text(code, encoding="utf-8")
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[-1500:]
    return json.loads(cp.stdout)


LIFECYCLE_FNS = ("function lifecycleBase(", "function lifecycleVisible(", "function lifecycleModel(",
                 "function lifecycleStageDone(", "function lifecycleNode(", "function lifecyclePrimaryAction(",
                 "function lifecycleStageProblems(", "function lifecycleChecklist(", "function lifecycleDate(",
                 "function lifecycleVersion(", "function lifecycleShortSha(", "function lifecycleEditable(",
                 "function hideLifecycleTip(", "function placeLifecycleTip(", "function showLifecycleTip(",
                 "function lifecycleTipAttach(", "function lifecycleTooltip(", "function lifecycleAction(",
                 "function lifecycleDownload(", "function lifecycleVersions(", "function drawLifecycleDetail(",
                 "function drawLifecycle(", "function lifecyclePlanSteps(", "function drawLifecyclePlan(",
                 "function renderLifecycleLoading(", "function renderLifecycleError(")

MODEL_CREATED = {
    "applicable": True, "path": SRC, "title": "US-1", "kind": "us", "kind_title": "История",
    "stage": "created", "current": "frozen", "next_version": "1.0", "freezeable": True,
    "created": {"exists": True, "at": "2026-10-01", "source": "git"},
    "publication": {"state": "none", "done": False, "url": "", "at": "", "stale": False},
    "frozen": [], "released_versions": [],
    "dependencies": {"created": [], "published": ["created"], "frozen": ["created"],
                     "released": ["created", "frozen"]},
    "required_for": {"freeze": ["created"], "release": ["created", "frozen"]},
    "optional": ["published"], "changes_after_freeze": False, "warnings": [], "problems": [],
}


def _model(**overrides) -> dict:
    model = json.loads(json.dumps(MODEL_CREATED))
    model.update(overrides)
    return model


def _js_model(model: dict) -> str:
    return json.dumps(model, ensure_ascii=False)


@test
def test_lifecycle_three_nodes_and_badge_outside_line(tmp: Path):
    """Линия ровно из трёх узлов; публикация — бейдж вне `ol`, не стадия и не четвёртый шаг."""
    model = _model(stage="frozen", current="released",
                   frozen=[{"version": "1.0", "at": "2026-10-19",
                            "path": "Deliverables/us/US-1_v1.0_2026-10-19.md", "sha256": "a" * 64, "ok": True}],
                   publication={"state": "published", "done": True, "at": "2026-10-20",
                                "url": "https://wiki.example.com/pages/viewpage.action?pageId=7", "stale": False})
    code = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
""" + _ui(*LIFECYCLE_FNS) + """
const model = """ + _js_model(model) + """;
drawLifecycle(model);
const line = $("ol.lifecycle-line", host);
const nodes = pickAll(".lifecycle-node", host);
const badge = $(".lifecycle-badge", host);
console.log(JSON.stringify({
  lis: line.kids.filter(k => k.tag === "li").length,
  nodes: nodes.length,
  labels: nodes.map(n => text(n)),
  stages: nodes.map(n => n.dataset.stage),
  badgeInLine: pickAll(".lifecycle-badge", line).length,
  badge: text(badge),
  describedby: badge.getAttribute("aria-describedby") || "",
  lineText: text(line),
  ariaCurrent: nodes.filter(n => n.getAttribute("aria-current"))
                    .map(n => [n.dataset.stage, n.getAttribute("aria-current")]),
  visible: host.hidden === false, stored: !!F.lifecycle,
}));
"""
    out = _node(code, tmp)
    assert out["lis"] == 3 and out["nodes"] == 3, f"линия не из трёх узлов: {out}"
    assert out["stages"] == ["created", "frozen", "released"], out
    assert out["labels"][0].startswith("files.lifecycle.created")
    assert out["labels"][1].startswith("files.lifecycle.frozen")
    assert out["labels"][2].startswith("files.lifecycle.released")
    assert out["badgeInLine"] == 0, "бейдж публикации попал в линию стадий"
    assert out["badge"] == "files.lifecycle.published", out
    assert out["describedby"], "у бейджа нет aria-describedby на подсказку"
    assert "files.lifecycle.published" not in out["lineText"], "публикация попала в тексты линии"
    assert out["ariaCurrent"] == [["released", "step"]], out
    assert out["visible"] and out["stored"], out

    # Выпущенный документ: `current` пуст, поэтому `aria-current` нет ни у одного узла.
    code2 = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
""" + _ui(*LIFECYCLE_FNS) + """
const model = """ + _js_model(_model(stage="released", current="")) + """;
drawLifecycle(model);
console.log(JSON.stringify({current: pickAll(".lifecycle-node", host).map(n => n.getAttribute("aria-current"))}));
"""
    out = _node(code2, tmp)
    assert out["current"] == [None, None, None], f"у выпущенного документа есть aria-current: {out}"


@test
def test_lifecycle_clicks_filled_and_empty(tmp: Path):
    """Клик по узлу показывает детали: заполненный — версию, дату, хеш; пустой — чек-лист обязательных шагов."""
    code = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
""" + _ui(*LIFECYCLE_FNS) + """
const model = """ + _js_model(_model()) + """;
drawLifecycle(model);
const click = stage => $(`.lifecycle-node[data-stage="${stage}"]`, host).dispatch("click");
const detail = () => text($("#fileLifecycleDetail"));
const out = {};
click("created");
out.created = detail();
out.createdExpanded = $('.lifecycle-node[data-stage="created"]', host).getAttribute("aria-expanded");
click("frozen");
out.frozen = detail();
out.frozenExpanded = $('.lifecycle-node[data-stage="frozen"]', host).getAttribute("aria-expanded");
out.createdCollapsed = $('.lifecycle-node[data-stage="created"]', host).getAttribute("aria-expanded");
click("released");
out.released = detail();
$(".lifecycle-badge", host).dispatch("click");
out.published = detail();
console.log(JSON.stringify(out));
"""
    out = _node(code, tmp)
    assert "2026-10-01" in out["created"] and SRC in out["created"], out["created"]
    assert "files.lifecycle.checklist" not in out["created"], "у заполненного узла чек-лист вместо деталей"
    assert out["createdExpanded"] == "true" and out["frozenExpanded"] == "true" \
        and out["createdCollapsed"] == "false", out
    for stage in ("created", "frozen", "released"):
        assert f"files.lifecycle.{stage}" in out["frozen"], f"в чек-листе нет шага {stage}: {out['frozen']}"
    assert "files.lifecycle.checklist" in out["frozen"] and "files.lifecycle.released" in out["released"], out
    assert "files.lifecycle.optional" in out["published"] and "files.lifecycle.empty" in out["published"], out


@test
def test_lifecycle_release_from_created(tmp: Path):
    """«Выпустить» из «Создан» — один клик: план (freeze -> export -> release), затем `ship:release` с `--export docx`."""
    plan = {"ok": True, "action": "both", "version": "1.0", "problems": [],
            "steps": [{"action": "freeze", "path": "Deliverables/us/US-1_v1.0_2026-10-19.md"},
                      {"action": "export", "path": "Deliverables/us/US-1_v1.0_2026-10-19.docx"},
                      {"action": "release"}]}
    code = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
""" + _ui(*LIFECYCLE_FNS) + _ui("async function runShip(", "function lifecycleDone(") + """
const plan = """ + _js_model(plan) + """;
REPLY = (path) => path === "/api/ship/plan" ? plan : {job: "j1"};
const model = """ + _js_model(_model()) + """;
drawLifecycle(model);
const actions = () => pickAll(".lifecycle-actions button", host).map(b => text(b));
const before = {actions: actions(), release: actions().includes("ship.release")};
const both = pickAll(".lifecycle-actions button", host).find(b => text(b) === "ship.both");
both.dispatch("click");
for (let i = 0; i < 12; i++) await Promise.resolve();
const calls = CALLS.map(([path, opts]) => [path, opts.method || "GET", opts.body ? JSON.parse(opts.body) : null]);
const out = {before, calls, polls: POLLS, plan: text($(".lifecycle-plan", host))};
// Заморозка отдельной кнопкой: у замороженного документа главное действие — выпуск.
CALLS.length = 0; POLLS.length = 0;
F.lifecycleBusy = false;
await runShip(model.path, "freeze", "1.1");
out.freezeCalls = CALLS.map(([path, opts]) => [path, opts.body ? JSON.parse(opts.body) : null]);
drawLifecycle(Object.assign({}, model, {stage: "frozen", current: "released"}));
out.frozenActions = actions();
console.log(JSON.stringify(out));
"""
    out = _node(code, tmp)
    assert "ship.both" in out["before"]["actions"], f"из created нет «Выпустить»: {out['before']}"
    assert not out["before"]["release"], "из created предлагается выпуск без заморозки"
    assert out["calls"][0][0].startswith("/api/ship/plan") and out["calls"][0][2]["action"] == "both", out["calls"]
    run = out["calls"][1]
    assert run[0] == "/api/run" and run[2]["cmd"] == "ship:release", run
    assert run[2]["args"] == [SRC, "--export", "docx", "--apply"], f"выпуск не идёт через ship:release/export: {run}"
    assert out["polls"] and out["polls"][0][2] == "ship:release " + SRC, out["polls"]
    assert "Deliverables/us/US-1_v1.0_2026-10-19.md" in out["plan"] and ".docx" in out["plan"], out["plan"]
    freeze = out["freezeCalls"][0]
    assert freeze[0] == "/api/run" and freeze[1]["args"] == [SRC, "--apply", "--version", "1.1"], freeze
    assert "ship.release" in out["frozenActions"] and "ship.both" not in out["frozenActions"], out["frozenActions"]


@test
def test_lifecycle_publish_is_optional(tmp: Path):
    """Публикация — необязательный бейдж: её нет в `required_for`, заморозка и выпуск доступны без неё."""
    code = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
""" + _ui(*LIFECYCLE_FNS) + """
const model = """ + _js_model(_model()) + """;
drawLifecycle(model);
const byKey = key => text(pickAll(".lifecycle-actions button", host).find(b => text(b) === key) || {kids: []});
console.log(JSON.stringify({
  primary: ["created", "frozen", "released"].map(s => {
    const a = lifecyclePrimaryAction(Object.assign({}, model,
      {stage: s, current: {created: "frozen", frozen: "released", released: ""}[s]}));
    return a && [a.action, a.label];
  }),
  required: model.required_for,
  optional: model.optional,
  checklist: lifecycleChecklist(model, "published"),
  publishButton: !!pickAll(".lifecycle-actions button", host).find(b => text(b) === "editor.publish"),
  freezeButton: !!pickAll(".lifecycle-actions button", host).find(b => text(b) === "ship.both"),
  badge: text($(".lifecycle-badge", host)),
}));
"""
    out = _node(code, tmp)
    assert out["primary"] == [["freeze", "ship.freeze"], ["release", "ship.release"], None], out["primary"]
    assert "published" not in out["required"]["freeze"] + out["required"]["release"], out
    assert out["optional"] == ["published"], out
    assert out["publishButton"] and out["freezeButton"], "шаги закрыты без публикации"
    assert out["badge"] == "files.lifecycle.optional", out
    published = [c for c in out["checklist"] if c["stage"] == "published"]
    assert published and published[0]["done"] is False, out["checklist"]


@test
def test_lifecycle_versions_and_old_releases(tmp: Path):
    """Все версии с датой, sha256 (полный в title), путём и `ok`; старый выпуск и потерянный docx видны."""
    long_sha = "b" * 64
    model = _model(stage="released", current="", next_version="1.2",
                   frozen=[{"version": "1.0", "at": "2026-10-19", "path": "Deliverables/us/US-1_v1.0.md",
                            "sha256": "a" * 64, "ok": True},
                           {"version": "1.1", "at": "2026-10-25", "path": "Deliverables/us/US-1_v1.1.md",
                            "sha256": long_sha, "ok": True}],
                   released_versions=[{"version": "1.0", "at": "2026-10-20",
                                       "path": "Deliverables/us/US-1_v1.0.docx", "sha256": "c" * 64,
                                       "frozen_path": "Deliverables/us/US-1_v1.0.md", "ok": True},
                                      {"version": "0.9", "at": "2026-05-05", "path": SRC,
                                       "sha256": "d" * 64, "legacy": True, "ok": False}],
                   problems=[{"code": "release-missing", "detail": "Deliverables/us/US-1_v1.0.docx"}])
    code = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
""" + _ui(*LIFECYCLE_FNS) + """
const model = """ + _js_model(model) + """;
drawLifecycle(model);
const box = lifecycleVersions(model);
const rows = pickAll("tbody tr", box.box).map(tr => tr.kids.map(td => text(td)));
const shaCell = pickAll("tbody tr", box.box)[1].kids[2];
const btn = pickAll(".lifecycle-actions button", host).find(b => text(b).startsWith("files.lifecycle.all_versions"));
const details = $("details.lifecycle-versions", host);
btn.dispatch("click");
console.log(JSON.stringify({
  count: box.count, rows, title: shaCell.getAttribute("title"), short: text(shaCell),
  summary: text($("summary", box.box)), button: text(btn), open: details.open,
  problems: text($(".note.warn", host)),
}));
"""
    out = _node(code, tmp)
    assert out["count"] == 4 and len(out["rows"]) == 4, out
    assert out["rows"][0][:4] == ["1.0", "2026-10-19", "a" * 12, "Deliverables/us/US-1_v1.0.md"], out["rows"][0]
    assert out["short"] == "b" * 12 and out["title"] == long_sha, "полный sha256 не сохранён в title"
    assert out["rows"][3][0] == "0.9" and out["rows"][3][4] == "files.lifecycle.error", out["rows"][3]
    assert out["summary"] == "files.lifecycle.all_versions (4)", out
    assert "(4)" in out["button"] and out["open"] is True, out
    assert "Deliverables/us/US-1_v1.0.docx" in out["problems"], out["problems"]


@test
def test_lifecycle_editor_returns_without_losing_changes(tmp: Path):
    """Возврат в редактор не теряет несохранённое: карточка не уводит от правок без подтверждения."""
    rel = SRC
    code = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
const editor = el("div", {id: "fileEditor", hidden: ""});
document.body.append(editor);
const none = el("div", {id: "fileNone"});
document.body.append(none);
""" + _ui(*LIFECYCLE_FNS) + _ui("async function openLifecycle(", "async function lifecycleRefresh(") + """
const model = """ + _js_model(_model()) + """;
REPLY = () => model;
const OPENED = [];
const openFile = f => { OPENED.push(f); };
const out = {};
F = {path: "Artifacts/us/другой.md", files: [], lifecycle: null, lifecyclePath: "", lifecyclePick: "",
     lifecycleBusy: false, dirty: true, ed: {}, publishable: true, createKind: "", reopen: ""};
CONFIRM = false;
await openLifecycle({path: """ + json.dumps(rel) + """, name: "US-1.md", text: true, applicable: true});
out.refused = {calls: CALLS.length, path: F.path, destroyed: F.ed === null};
CONFIRM = true;
await openLifecycle({path: """ + json.dumps(rel) + """, name: "US-1.md", text: true, applicable: true});
out.opened = {calls: CALLS.length, path: F.path, lifecycle: !!F.lifecycle,
              editorHidden: $("#fileEditor").hidden, cardHidden: $("#fileLifecycle").hidden};
const edit = pickAll(".lifecycle-head button", host).find(b => text(b) === "files.lifecycle.edit");
edit.dispatch("click");
out.edit = OPENED[0] || null;
console.log(JSON.stringify(out));
"""
    out = _node(code, tmp)
    assert out["refused"] == {"calls": 0, "path": "Artifacts/us/другой.md", "destroyed": False}, \
        f"карточка увела из редактора с несохранённым: {out['refused']}"
    assert out["opened"]["calls"] == 1 and out["opened"]["path"] == rel and out["opened"]["lifecycle"], out["opened"]
    assert out["opened"]["editorHidden"] is True and out["opened"]["cardHidden"] is False, out["opened"]
    assert out["edit"]["path"] == rel and out["edit"]["text"] is True, f"«Редактировать» открывает не тот файл: {out['edit']}"
    ui = panel_sources()
    head = _js_function(ui, "async function openFile(")
    assert "F.ed && F.dirty && !confirm(" in head, "openFile больше не защищает несохранённое"
    assert 'if ($("#fileLifecycle") && !$("#fileLifecycle").hidden)' in head and "F.lifecycle = null" in head, \
        "редактор не прячет карточку после возврата"


@test
def test_lifecycle_tooltip_hover_focus_touch_escape(tmp: Path):
    """Подсказка: 500 мс на наведение, мгновенное скрытие, фокус, долгое нажатие, Escape, `aria-describedby`."""
    model = _model(stage="frozen", current="released", changes_after_freeze=True,
                   frozen=[{"version": "1.0", "at": "2026-10-19", "by": "Вадим",
                            "path": "Deliverables/us/US-1_v1.0.md", "sha256": "a" * 64, "ok": True}],
                   problems=[{"code": "snapshot-missing", "detail": "Deliverables/us/US-1_v1.0.md"}])
    code = DOM_STUBS + _ui(*LIFECYCLE_FNS) + """
const model = """ + _js_model(model) + """;
const node = el("button", {});
const box = lifecycleTipAttach(node, "подсказка");
const out = {attrs: {describedby: node.getAttribute("aria-describedby"), id: box.getAttribute("id"),
                     role: box.getAttribute("role"), hidden: box.hidden}};
node.dispatch("mouseenter");
out.delay = TIMERS.length ? TIMERS[TIMERS.length - 1].ms : -1;
out.before = box.hidden;
runTimers(); out.afterHover = box.hidden;
node.dispatch("mouseleave"); out.afterLeave = box.hidden;
node.dispatch("focus"); runTimers(); out.afterFocus = box.hidden;
node.dispatch("blur"); out.afterBlur = box.hidden;
node.dispatch("touchstart"); runTimers(); out.afterTouch = box.hidden;
node.dispatch("touchend"); out.afterTouchEnd = box.hidden;
node.dispatch("keydown", {key: "Enter", preventDefault(){}, stopPropagation(){}}); out.afterEnter = box.hidden;
node.dispatch("keydown", {key: "Escape"}); out.afterEscape = box.hidden;
node.dispatch("mouseenter"); const pending = TIMERS.filter(x => x.fn).length;
node.dispatch("mouseleave"); runTimers(); out.leaveClears = {pending, hidden: box.hidden};
node.getBoundingClientRect = () => ({top: 700, left: 150, bottom: 740, width: 20, height: 40});
box.getBoundingClientRect = () => ({width: 120, height: 100});
placeLifecycleTip(node, box);
out.place = {left: box.style.left, top: box.style.top};
out.created = lifecycleTooltip(model, "created");
out.frozen = lifecycleTooltip(model, "frozen");
out.released = lifecycleTooltip(model, "released");
out.published = lifecycleTooltip(model, "published");
console.log(JSON.stringify(out));
"""
    out = _node(code, tmp)
    assert out["attrs"]["role"] == "tooltip" and out["attrs"]["hidden"] is True, out
    assert out["attrs"]["describedby"] == out["attrs"]["id"], "aria-describedby не связан с подсказкой"
    assert out["delay"] == 500, f"подсказка ждёт {out['delay']} мс вместо 500"
    assert out["before"] is True and out["afterHover"] is False, "наведение не показывает подсказку"
    assert out["afterLeave"] is True and out["afterFocus"] is False and out["afterBlur"] is True, out
    assert out["afterTouch"] is False and out["afterTouchEnd"] is True, "на тач-экране нет долгого нажатия"
    assert out["afterEnter"] is False and out["afterEscape"] is True, "Escape не прячет подсказку"
    assert out["leaveClears"]["pending"] >= 1 and out["leaveClears"]["hidden"] is True, "уход не отменяет таймер"
    assert out["place"]["top"].endswith("px") and int(out["place"]["top"][:-2]) >= 8, out["place"]
    assert int(out["place"]["left"][:-2]) <= 200 - 120 - 8, f"подсказка уходит за край: {out['place']}"
    assert "files.lifecycle.frozen" in out["frozen"] and "2026-10-19" in out["frozen"] \
        and "Deliverables/us/US-1_v1.0.md" in out["frozen"], out["frozen"]
    assert "files.lifecycle.changes_after_freeze" in out["frozen"], out["frozen"]
    assert "files.lifecycle.problems" in out["frozen"], out["frozen"]


@test
def test_lifecycle_keyboard_aria_contrast(tmp: Path):
    """Стрелки водят фокус по линии, Enter/Space не перехвачены, `aria-label` называет состояние, контраст 4.5:1."""
    code = DOM_STUBS + """
const host = el("div", {id: "fileLifecycle"});
document.body.append(host);
""" + _ui(*LIFECYCLE_FNS) + """
const model = """ + _js_model(_model()) + """;
drawLifecycle(model);
const line = $("ol.lifecycle-line", host);
const nodes = pickAll(".lifecycle-node", host);
let prevented = [];
const key = (k) => line.dispatch("keydown", {key: k, preventDefault(){ prevented.push(k); }});
nodes[0].focus();
key("ArrowRight");
const right = document.activeElement;
key("ArrowLeft");
const left = document.activeElement;
key("Enter");
key(" ");
console.log(JSON.stringify({
  right: right && right.dataset.stage, left: left && left.dataset.stage, prevented,
  current: pickAll(".lifecycle-node[aria-current]", host).map(n => n.dataset.stage),
  labels: nodes.map(n => n.getAttribute("aria-label")),
  types: nodes.map(n => n.getAttribute("type") || ""),
  tabindex: nodes.map(n => n.getAttribute("tabindex")),
}));
"""
    out = _node(code, tmp)
    assert out["right"] == "frozen" and out["left"] == "created", f"стрелки не водят фокус: {out}"
    assert out["prevented"] == ["ArrowRight", "ArrowLeft"], f"Enter/Space перехвачены: {out['prevented']}"
    assert out["current"] == ["frozen"], out["current"]
    assert all(t == "button" for t in out["types"]), out
    assert "files.lifecycle.done" in out["labels"][0], out["labels"][0]
    assert "files.lifecycle.empty" in out["labels"][1] and "ship.freeze" in out["labels"][1], out["labels"][1]
    assert "ship.release" in out["labels"][2], out["labels"][2]
    assert out["tabindex"] == [None, None, None], "узлы выпали из порядка табуляции"

    css = (KIT / "cockpit/ui/panel.css").read_text(encoding="utf-8")
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in css, "линия не из трёх колонок"
    assert ".lifecycle-node{min-height:44px" in css, "цель нажатия меньше 44px"
    assert ".lifecycle-node:focus-visible{outline:2px solid" in css, "нет focus-visible у узла"
    assert "@media(max-width:640px){.lifecycle-line,.lifecycle-actions{grid-template-columns:1fr}" in css, \
        "на узком экране линия не в одну колонку"
    assert "@media(max-width:900px){.lifecycle-actions{display:grid;grid-template-columns:repeat(2" in css, \
        "на среднем экране кнопки не в две колонки"
    assert "@media(prefers-reduced-motion:reduce)" in css and ".lifecycle-node:active{transform:none}" in css, \
        "нет неподвижного варианта для prefers-reduced-motion"
    node_rule = re.search(r"\.lifecycle-node\{[^}]*\}", css).group(0)
    assert "color:var(--text)" in node_rule and "background:var(--surface-1)" in node_rule, \
        f"цвет узла не из токенов скини: {node_rule}"
    assert not re.search(r"#[0-9a-fA-F]{6}\b", node_rule), f"hex-литерал в правиле узла: {node_rule}"
    tokens = dict(re.findall(r"--(text|surface-1|text-muted):\s*(#[0-9a-fA-F]{6})", css))
    assert {"text", "surface-1", "text-muted"} <= set(tokens), tokens
    for fg, bg, minimum in (("text", "surface-1", 4.5), ("text-muted", "surface-1", 4.5)):
        ratio = _contrast(tokens[fg], tokens[bg])
        assert ratio >= minimum, f"контраст --{fg} на --{bg} = {ratio:.2f} < {minimum}"


def _contrast(a: str, b: str) -> float:
    def lum(hex_value: str) -> float:
        channels = [int(hex_value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    one, two = sorted((lum(a), lum(b)), reverse=True)
    return (one + 0.05) / (two + 0.05)


@test
def test_lifecycle_i18n_both_languages(_t):
    """Строки карточки есть в обоих каталогах, английские без русского и без длинных тире."""
    keys = """files.lifecycle.created files.lifecycle.frozen files.lifecycle.released
files.lifecycle.published files.lifecycle.optional files.lifecycle.current files.lifecycle.done
files.lifecycle.empty files.lifecycle.warning files.lifecycle.error files.lifecycle.unavailable
files.lifecycle.changes_after_freeze files.lifecycle.all_versions files.lifecycle.version
files.lifecycle.sha files.lifecycle.problems files.lifecycle.checklist files.lifecycle.edit
files.lifecycle.reveal files.lifecycle.download files.lifecycle.open_docx files.lifecycle.open_snapshot
files.lifecycle.deliverable_later ship.freeze ship.release ship.both ship.plan ship.plan_empty
ship.done ship.problems ship.failed ship.confirm_release""".split()
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    missing = [k for k in keys if not ru.get(k)]
    assert not missing, f"нет русских строк: {missing}"
    missing = [k for k in keys if not en.get(k)]
    assert not missing, f"нет английских строк: {missing}"
    cyr = re.compile("[а-яА-ЯёЁ]")
    for key in keys:
        assert not cyr.search(en[key]), f"русский текст в английском каталоге: {key}: {en[key]}"
        for dash in ("\u2014", "\u2013"):
            assert dash not in ru[key] and dash not in en[key], f"длинное тире в строке {key}"
    ui = panel_sources()
    for key in keys:
        assert key in ui, f"строка {key} заведена, но карточка её не зовёт"
    assert not any(k.startswith("files.lifecycle.") and k not in ru for k in en), "ключи разошлись между каталогами"


@test
def test_lifecycle_node_released_is_not_null(tmp: Path):
    """`lifecycleNode` отдаёт записи, а не пустоту: выпуск и старая запись читаются карточкой."""
    code = DOM_STUBS + _ui(*LIFECYCLE_FNS) + """
const src = """ + json.dumps(SRC) + """;
const released = [{version: "1.0", path: "Deliverables/us/US-1_v1.0.docx", ok: true}];
const legacy = [{version: "0.9", path: src, legacy: true, ok: true}];
const empty = {applicable: true, path: src, created: {exists: true}, frozen: [],
               released_versions: [], current: "frozen", stage: "created",
               required_for: {freeze: ["created"], release: ["created", "frozen"]}, dependencies: {}};
console.log(JSON.stringify({
  released: lifecycleNode("released", Object.assign({}, empty, {released_versions: released})),
  legacy: lifecycleNode("released", Object.assign({}, empty, {released_versions: legacy})),
  frozen: lifecycleNode("frozen", empty),
  created: lifecycleNode("created", empty),
  done: [lifecycleStageDone(Object.assign({}, empty, {released_versions: released}), "released"),
         lifecycleStageDone(empty, "released")],
}));
"""
    out = _node(code, tmp)
    assert out["released"] and out["released"][0]["version"] == "1.0", out
    assert out["legacy"] and out["legacy"][0]["legacy"] is True, "старая запись выпуска не читается"
    assert out["frozen"] is None and out["created"]["exists"] is True, out
    assert out["done"] == [True, False], out


@test
def test_lifecycle_create_artifact_replaces_write(tmp: Path):
    """«Создать артефакт» — вход в производство: вид и идея уходят `agent:make`, режим `work` тот же."""
    kinds = {"us": {"title": "История", "out": "Artifacts/us"},
             "deliverable": {"title": "Поставка", "out": "Deliverables/work", "deliverable": "true"}}
    code = DOM_STUBS + """
const create = el("div", {id: "fileCreate", hidden: ""});
document.body.append(create);
const lifecycle = el("div", {id: "fileLifecycle", hidden: ""});
document.body.append(lifecycle);
const editor = el("div", {id: "fileEditor", hidden: ""});
document.body.append(editor);
const none = el("div", {id: "fileNone"});
document.body.append(none);
""" + _ui("async function openCreateArtifact(") + """
REPLY = (path) => path.startsWith("/api/kinds") ? {kinds: """ + _js_model(kinds) + """}
  : {job: "j2"};
await openCreateArtifact();
const host = $("#fileCreate");
const select = $("select", host);
const idea = $("textarea", host);
const go = pickAll("button", host).find(b => text(b) === "files.lifecycle.create");
const out = {shown: SHOWN.slice(), hidden: {create: host.hidden, lifecycle: $("#fileLifecycle").hidden,
                                             editor: $("#fileEditor").hidden},
              options: pickAll("option", host).map(o => [o.getAttribute("value"), text(o)]), go: !!go};
select.value = "deliverable";
select.dispatch("change");
const note = pickAll(".note", host)[0];
out.deliverable = {hidden: note.hidden, text: note.textContent, disabled: go.disabled};
select.value = "us";
idea.value = "собрать историю приёмки";
go.dispatch("click");
for (let i = 0; i < 8; i++) await Promise.resolve();
out.runs = CALLS.filter(([p]) => p === "/api/run").map(([, o]) => JSON.parse(o.body));
out.polls = POLLS; out.shownAfter = SHOWN.slice();
console.log(JSON.stringify(out));
"""
    out = _node(code, tmp)
    assert out["hidden"] == {"create": False, "lifecycle": True, "editor": True}, out["hidden"]
    assert out["options"] == [["us", "История"], ["deliverable", "Поставка"]], out["options"]
    assert out["go"] is True, "нет кнопки «Создать артефакт»"
    assert out["deliverable"]["hidden"] is False and "files.lifecycle.deliverable_later" in out["deliverable"]["text"], \
        f"вид с deliverable молчит о режиме: {out['deliverable']}"
    assert out["deliverable"]["disabled"] is False, "кнопку погасили молча"
    assert out["runs"] == [{"project": "/p", "cmd": "agent:make",
                            "args": ["--kind", "us", "--idea", "собрать историю приёмки"]}], out["runs"]
    assert out["polls"] and out["polls"][0][2] == "agent:make us", out["polls"]
    assert out["shownAfter"][-1] == "work", f"после запуска не открывается производство: {out['shownAfter']}"
    html = (KIT / "cockpit/ui/index.html").read_text(encoding="utf-8")
    assert 'id="fileCreateButton"' in html and 'data-i18n="files.lifecycle.create"' in html, "входа «Создать артефакт» нет"
    ui = panel_sources()
    assert 'fileCreateButton' in ui and "openCreateArtifact" in ui, "кнопка входа не связана с карточкой"
