"""Проверки движка Aurora, часть 43. Каркас и помощники — tests/harness.py.

Единый конверт манифеста расширений (BL-ARC-44), срез 1: общий загрузчик
`aurora_common.load_manifest` (мини-схема на вид, версия API, отчёт вместо исключения) и
первый потребитель — `sources_registry`: кривой `connector.json` пропускается строкой в отчёт,
а не обрушивает чтение реестра, на котором стоят `update`, doctor и панель.
Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _connector(cid: str = "demo", **over) -> dict:
    m = {"id": cid, "title": "Демо", "kind": "board", "role": "tasks",
         "mirror": {"default_path": f"Sources/{cid}", "state": "update_log.md"},
         "run": {"script": f"{cid}_export.py", "command": f"sync:{cid}", "skill": f"{cid}-sync"},
         "auth": {"env_prefix": cid.upper()}, "requires": {"python": []}}
    m.update(over)
    return m


@test
def test_extension_loader_never_raises(tmp: Path):
    """`load_manifest` на любом входе возвращает (None, причина), а не исключение."""
    import aurora_common as C
    bad = {
        "нет файла": tmp / "nope.json",
        "каталог вместо файла": tmp,
        "не JSON": _write(tmp / "a.json", "{не json"),
        "корень — список": _write(tmp / "b.json", "[1, 2]"),
        "корень — строка": _write(tmp / "c.json", '"x"'),
        "пустой файл": _write(tmp / "d.json", ""),
        "не UTF-8": tmp / "e.json",
        "нет id": _write(tmp / "f.json", {"kind": "wiki", "mirror": {}, "run": {}}),
        "пустой id": _write(tmp / "g.json", _connector("x", id="  ")),
    }
    (tmp / "e.json").write_bytes(b"\xff\xfe\x00{")
    for what, path in bad.items():
        got, rep = C.load_manifest(path, "connector")
        assert got is None and rep and all(isinstance(r, str) for r in rep), (what, got, rep)
    got, rep = C.load_manifest(None, "connector")
    assert got is None and rep, "None вместо пути уронил загрузчик"
    got, rep = C.load_manifest(tmp / "a.json", "no-such-kind")
    assert got is None and "no-such-kind" in rep[0], "неизвестный вид расширения не назван"


@test
def test_extension_loader_schema_and_api_version(tmp: Path):
    """Схема вида: типы полей и вложенных объектов; `api_version` — нет в файле значит 1, из будущего — пропуск."""
    import aurora_common as C
    # старый манифест без api_version читается как v1 и ничего не теряет
    old = _write(tmp / "old.json", _connector("old"))
    got, rep = C.load_manifest(old, "connector")
    assert rep == [] and got["api_version"] == 1 and got["id"] == "old" \
        and got["mirror"]["default_path"] == "Sources/old", (got, rep)
    assert "api_version" not in json.loads(old.read_text(encoding="utf-8")), "загрузчик переписал файл"
    # BOM (так пишет Блокнот) не делает манифест нечитаемым
    bom = tmp / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(_connector("bom")).encode("utf-8"))
    assert C.load_manifest(bom, "connector")[0]["id"] == "bom"
    # версия API
    new = _write(tmp / "new.json", _connector("new", api_version=C.EXT_API_VERSION + 1))
    got, rep = C.load_manifest(new, "connector")
    assert got is None and "new" in rep[0] and "API" in rep[0], rep
    for v in (0, "1", True, 1.5, None):
        got, rep = C.load_manifest(_write(tmp / "v.json", _connector("v", api_version=v)), "connector")
        assert got is None and "api_version" in rep[0], (v, rep)
    ok, _ = C.load_manifest(_write(tmp / "v1.json", _connector("v1", api_version=1)), "connector")
    assert ok and ok["api_version"] == 1
    # типы полей и вложенных объектов
    cases = {
        "mirror": _connector("t1", mirror="Sources/x"),
        "mirror.default_path": _connector("t2", mirror={"default_path": 5}),
        "run": _connector("t3", run=["a"]),
        "run.command": _connector("t4", run={"command": ["x"]}),
        "role": _connector("t5", role=3),
        "id": _connector("t6", id=7),
        "requires": _connector("t7", requires="python"),
    }
    for field, data in cases.items():
        got, rep = C.load_manifest(_write(tmp / "t.json", data), "connector")
        assert got is None and field in rep[0], (field, got, rep)
    # модуль панели: имя — строка или по языкам, группа — строка; неверный тип — пропуск с причиной
    mod = {"id": "m1", "name": {"ru": "Раздел", "en": "Section"}, "group": "project", "order": 3,
           "needs": [], "commands": [], "for": "1.173.0"}
    assert C.load_manifest(_write(tmp / "module.json", mod), "module")[0]["id"] == "m1"
    for over in ({"name": 5}, {"name": {"ru": 5}}, {"order": True}, {"group": ["x"]}, {"needs": "kit"}):
        got, rep = C.load_manifest(_write(tmp / "module.json", {**mod, **over}), "module")
        assert got is None and rep, (over, got)
    got, rep = C.load_manifest(_write(tmp / "module.json", {"id": "m1"}), "module")
    assert got is None and "name" in rep[0], rep
    # неизвестные поля не мешают: схема не запрещает лишнего
    extra = _connector("x1", future_field={"a": 1})
    assert C.load_manifest(_write(tmp / "x.json", extra), "connector")[0]["future_field"] == {"a": 1}


@test
def test_sources_registry_survives_broken_connectors(tmp: Path):
    """Один кривой `connector.json` не роняет реестр: он пропущен строкой в отчёт, остальные читаются."""
    import sources_registry as R
    R._REPORT.clear()
    root = tmp / "proj"
    conn = root / ".aurora" / "connectors"
    _write(conn / "good.json", _connector("good"))
    _write(conn / "old.json", _connector("old", kind="wiki", role="artifacts"))
    _write(conn / "root_list.json", "[1, 2, 3]")
    _write(conn / "no_id.json", {"kind": "wiki", "mirror": {"default_path": "Sources/X"}, "run": {}})
    _write(conn / "bad_role.json", _connector("bad-role", role="oracle"))
    _write(conn / "bad_kind.json", _connector("bad-kind", kind="chat"))
    _write(conn / "no_default.json", _connector("no-default", mirror={"legacy_path_key": "sources_nd"}))
    _write(conn / "mirror_str.json", _connector("mirror-str", mirror="Sources/S"))
    _write(conn / "future.json", _connector("future", api_version=99))
    _write(conn / "garbage.json", "{")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        mods = R.installed(str(root))
        # роль неизвестна: пропуск возвращает {}, как остальные ветки, а не False
        assert R.load(str(conn / "bad_role.json")) == {}, "неизвестная роль вернула не {}"
        assert R.load(str(conn / "root_list.json")) == {}
        # legacy и instances на манифесте без mirror.default_path — без KeyError
        (root / "aurora.config.yaml").write_text("sources_nd: Sources/ND\n", encoding="utf-8")
        legacy = R.legacy(str(root), mods)
        inst = R.instances(str(root))
    assert set(mods) == {"good", "old", "no-default"}, sorted(mods)
    assert [x["module"] for x in legacy] == ["no-default"] and legacy[0]["path"] == "Sources/ND", legacy
    assert {i["module"] for i in inst} == {"no-default"}, inst
    rep = "\n".join(R.registry_report())
    for needle in ("root_list", "no_id", "bad-role", "bad-kind", "mirror-str", "future", "garbage"):
        assert needle in rep, (needle, rep)
    assert "oracle" in rep and "KeyError" not in rep, rep
    assert err.getvalue().strip(), "причины пропуска не дошли до stderr"
    # без пути в конфиге и без default_path зеркалу негде жить — модуль не выбрасывается, но и не падает
    (root / "aurora.config.yaml").write_text("", encoding="utf-8")
    with contextlib.redirect_stderr(io.StringIO()):
        assert R.legacy(str(root), mods) == []
        decl = root / "aurora.config.yaml"
        decl.write_text("sources:\n  - id: ND\n    module: no-default\n", encoding="utf-8")
        got = R.instances(str(root))
    assert got and got[0]["module"] == "no-default" and got[0]["path"] == "", got
    # повтор вызова не множит одинаковые строки отчёта
    n = len(R.registry_report())
    with contextlib.redirect_stderr(io.StringIO()):
        R.installed(str(root))
    assert len(R.registry_report()) == n, "одна и та же причина записана дважды"


@test
def test_sources_registry_cli_keeps_valid_modules_next_to_broken(tmp: Path):
    """Команда реестра на проекте с кривыми манифестами не падает и показывает годные модули."""
    root = make_project(tmp)
    conn = root / ".aurora" / "connectors"
    _write(conn / "root_list.json", "[]")
    _write(conn / "no_id.json", {"kind": "wiki"})
    _write(conn / "no_default.json", _connector("no-default", mirror={}))
    _write(conn / "future.json", _connector("future", api_version=42))
    cp = run("sources_registry.py", cwd=root, expect_rc=0)
    assert "confluence-dc" in cp.stdout and "jira-dc" in cp.stdout, cp.stdout
    assert "Traceback" not in cp.stderr and "KeyError" not in cp.stderr, cp.stderr
    assert "future" in cp.stderr, "причина пропуска не названа"
    js = json.loads(run("sources_registry.py", "--json", cwd=root, expect_rc=0).stdout)
    ids = {m["id"] for m in js["installed"]}
    assert {"confluence-dc", "jira-dc", "no-default"} <= ids and "future" not in ids, ids


@test
def test_sources_registry_reads_with_old_project_common(tmp: Path):
    """`update` из копии старого выпуска: её `aurora_common` без загрузчика конверта реестр не роняет."""
    import aurora_common as C
    import sources_registry as R
    conn = tmp / "proj" / ".aurora" / "connectors"
    _write(conn / "good.json", _connector("good"))
    _write(conn / "bad.json", "[1]")
    keep = C.load_manifest
    del C.load_manifest      # так выглядит `aurora_common` прошлого выпуска в памяти
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            mods = R.installed(str(tmp / "proj"))
    finally:
        C.load_manifest = keep
    assert set(mods) == {"good"}, sorted(mods)
