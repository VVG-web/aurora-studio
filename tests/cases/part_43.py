"""Проверки движка Aurora, часть 43. Каркас и помощники — tests/harness.py.

Единый конверт манифеста расширений (BL-ARC-44), срез 1: общий загрузчик
`aurora_common.load_manifest` (мини-схема на вид, версия API, отчёт вместо исключения) и
первый потребитель — `sources_registry`: кривой `connector.json` пропускается строкой в отчёт,
а не обрушивает чтение реестра, на котором стоят `update`, doctor и панель.
Срез 3: разделы панели (`cockpit.modules()`) читает тот же загрузчик в «мягком» режиме.
Срез 4: скины (`cockpit.skins()`: поле `api`, общая `ext_behind`, битый файл — запись об ошибке) и
`kit_i18n` (кривой module.json или connector.json — строка в отчёте проверки, не traceback).
Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
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


# ---------------------------------------------------------------------------------------------
# Срез 2: `update` и `install` раскладывают модули источников через загрузчик конверта: кривой
# `connector.json` кита — строка в отчёт и пропуск одного модуля, а не падение всего прогона.

def _mini_kit(tmp: Path) -> Path:
    """Пробный кит с одним правилом манифеста `(connectors)`: годный модуль и пять кривых.

    Скрипты — копии настоящих (update запускается ИЗ этого кита: `find_kit` берёт `parents[1]`).
    """
    import shutil
    from harness import KIT
    kit = tmp / "mini-kit"
    (kit / "scripts").mkdir(parents=True)
    for s in (KIT / "scripts").glob("*.py"):
        shutil.copy(s, kit / "scripts" / s.name)
    shutil.copy(KIT / "structure_dirs.txt", kit / "structure_dirs.txt")
    (kit / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (kit / "engine_manifest.txt").write_text("connectors => (connectors)\n", encoding="utf-8")
    con = kit / "connectors"
    _write(con / "demo" / "connector.json", _connector("demo"))
    (con / "demo" / "demo_export.py").write_text("print('demo')\n", encoding="utf-8")
    _write(con / "no_id" / "connector.json", {"kind": "wiki", "mirror": {}, "run": {}})
    _write(con / "empty" / "connector.json", "")
    _write(con / "run_str" / "connector.json", _connector("run-str", run="export.py"))
    _write(con / "future" / "connector.json", _connector("future", api_version=99))
    _write(con / "evil" / "connector.json", _connector("../evil"))
    _write(con / "evil_script" / "connector.json",
           _connector("evil-script", run={"script": "../../outside.py"}))
    # порядок обхода — по имени папки: годный модуль лежит и до, и после кривых
    _write(con / "zeta" / "connector.json", _connector("zeta"))
    return kit


def _run_update(kit: Path, root: Path, *args):
    import subprocess
    return subprocess.run([sys.executable, str(kit / "scripts" / "aurora_update.py"), str(root), *args],
                          cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace")


@test
def test_update_survives_broken_connector_manifests(tmp: Path):
    """`update` с кривыми `connector.json` в ките: код 0, пропуск назван, годные модули разложены."""
    kit = _mini_kit(tmp)
    root = make_project(tmp)
    # проект без модулей: всё, что лежит в .aurora/connectors, приедет только из кита
    for old in (root / ".aurora" / "connectors").glob("*.json"):
        old.unlink()
    dry = _run_update(kit, root)
    assert dry.returncode == 0, (dry.returncode, dry.stdout, dry.stderr)
    assert "Traceback" not in dry.stderr and "KeyError" not in dry.stderr, dry.stderr
    out = dry.stdout
    assert out.count("Модуль источников пропущен") == 6, out
    for needle in ("no_id", "empty", "run-str", "future", "../evil", "evil-script"):
        assert needle in out, (needle, out)
    assert ".aurora/connectors/demo.json" in out and ".aurora/connectors/zeta.json" in out, out
    assert not (root / ".aurora" / "connectors" / "demo.json").exists(), "dry-run записал файл"
    done = _run_update(kit, root, "--apply")
    assert done.returncode == 0, (done.stdout, done.stderr)
    conn = root / ".aurora" / "connectors"
    assert sorted(p.name for p in conn.glob("*.json")) == ["demo.json", "zeta.json"], list(conn.iterdir())
    assert (root / ".aurora" / "scripts" / "demo_export.py").is_file(), "скрипт годного модуля не доехал"
    assert not (tmp / "evil.json").exists() and not (root / ".aurora" / "evil.json").exists(), \
        "id с «..» вышел из .aurora/connectors"
    # повторный прогон: пропуски снова названы, а изменений уже нет
    again = _run_update(kit, root)
    assert again.returncode == 0 and again.stdout.count("Модуль источников пропущен") == 6, again.stdout


@test
def test_update_valid_connectors_keep_the_same_plan(tmp: Path):
    """Годные модули настоящего кита раскладываются как раньше: ни одного «пропущен» на dry-run."""
    from harness import KIT
    root = make_project(tmp)
    cp = _run_update(KIT, root)
    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    assert "Модуль источников пропущен" not in cp.stdout, cp.stdout
    assert ".aurora/connectors/" not in cp.stdout, "годные манифесты кита уже в проекте: плана быть не должно"


@test
def test_install_skips_broken_connector_manifests(tmp: Path):
    """`install` ставит годные модули и пропускает кривые строкой в отчёте; файл по `id` с «..» не создаётся."""
    import install_aurora as IA
    kit = _mini_kit(tmp)
    target = tmp / "installed"
    target.mkdir()
    keep = IA.KIT_ROOT
    IA.KIT_ROOT = kit
    try:
        inst = IA.Installer(target, "Demo", "Demo", None, None, force=False, dry_run=False)
        with contextlib.redirect_stdout(io.StringIO()):
            inst.install_connectors()
    finally:
        IA.KIT_ROOT = keep
    conn = target / ".aurora" / "connectors"
    assert sorted(p.name for p in conn.glob("*.json")) == ["demo.json", "zeta.json"], list(conn.iterdir())
    assert (target / ".aurora" / "scripts" / "demo_export.py").is_file()
    assert (target / "Sources" / "demo").is_dir(), "папка зеркала годного модуля не создана"
    assert "skip (скрипт не найден): zeta" in "\n".join(inst.actions), "модуль без скрипта не назван"
    log = "\n".join(inst.actions)
    assert log.count("skip (манифест не принят)") == 6, log
    for needle in ("no_id", "empty", "run-str", "future", "../evil", "evil-script"):
        assert needle in log, (needle, log)
    assert not (tmp / "evil.json").exists() and not (target / ".aurora" / "evil.json").exists()
    assert not (tmp / "outside.py").exists()


@test
def test_connector_manifest_name_safety(tmp: Path):
    """`load_connector_manifest`: id, script и skill — простые имена файлов; остальное — пропуск с причиной."""
    import aurora_common as C
    ok, rep = C.load_connector_manifest(_write(tmp / "ok.json", _connector("demo")))
    assert ok and not rep, (ok, rep)
    for bad in ("../x", "a/b", "a\\b", ".hidden", "x..y", "-x", "a b"):
        got, rep = C.load_connector_manifest(_write(tmp / "b.json", _connector(bad)))
        assert got is None and rep, (bad, got)
    for field in ("script", "skill"):
        got, rep = C.load_connector_manifest(
            _write(tmp / "b.json", _connector("demo", run={field: "../../x"})))
        assert got is None and field in rep[0], (field, rep)
    assert C.load_connector_manifest(tmp / "missing.json")[0] is None


@test
def test_doctor_names_skipped_connector_manifests(tmp: Path):
    """Doctor одной строкой называет манифест модуля, который реестр пропустил, а не молчит о нём."""
    root = make_project(tmp)
    clean = run("aurora_doctor.py", cwd=root)
    assert "манифест модуля источников не принят" not in clean.stdout, clean.stdout
    conn = root / ".aurora" / "connectors"
    _write(conn / "no_id.json", {"kind": "wiki", "mirror": {}, "run": {}})
    _write(conn / "future.json", _connector("future", api_version=42))
    _write(conn / "garbage.json", "{")
    cp = run("aurora_doctor.py", cwd=root)
    line = [l for l in cp.stdout.splitlines() if "манифест модуля источников не принят" in l]
    assert len(line) == 1 and line[0].startswith("WARN:") and "(3)" in line[0], cp.stdout
    assert "Traceback" not in cp.stderr, cp.stderr


# ---------------------------------------------------------------- срез 3: модули панели


def _modules_before(ck, root: Path) -> list:
    """Прежний `modules()` (до перевода на загрузчик), как эталон для годных манифестов."""
    out = []
    for name in sorted(p.name for p in root.iterdir()):
        man = root / name / "module.json"
        if not man.is_file():
            continue
        m = json.loads(man.read_text(encoding="utf-8"))
        ver = str(m.get("for") or ck.kit_version())
        out.append({
            "id": name, "name": m["name"],
            "group": m.get("group") if m.get("group") in ck.MODULE_GROUPS else "project",
            "order": int(m.get("order") or 100),
            "needs": [x for x in (m.get("needs") or []) if x in ("project", "kit")],
            "commands": list(m.get("commands") or []),
            "for": ver, "behind": ".".join(ver.split(".")[:2]) != ".".join(ck.kit_version().split(".")[:2]),
            "about": m.get("about", {}), "dev": m.get("dev") is True})
    out.sort(key=lambda m: (ck.MODULE_GROUPS.index(m["group"]), m["order"], m["id"]))
    return out


def _ck_modules_dir(path):
    """Панель, у которой папка разделов — `path`; вернуть (модуль, откат)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    keep = ck.MODULES_DIR
    ck.MODULES_DIR = str(path)

    def restore():
        ck.MODULES_DIR = keep
    return ck, restore


@test
def test_cockpit_modules_same_for_valid_manifests(tmp: Path):
    """Годные манифесты кита читаются как прежде: тот же список, без `warn` и `error`."""
    real = Path(__file__).resolve().parents[2] / "cockpit" / "modules"
    ck, restore = _ck_modules_dir(real)
    try:
        got = ck.modules()
    finally:
        restore()
    assert len(got) == len(list(real.glob("*/module.json"))) >= 17, [m["id"] for m in got]
    assert not [m for m in got if "error" in m or "warn" in m], [m for m in got if "error" in m or "warn" in m]
    assert got == _modules_before(ck, real)


@test
def test_cockpit_modules_survive_bad_manifests(tmp: Path):
    """Кривой `module.json` не роняет `modules()`: нестрогое — умолчание и `warn`, обязательное — `error`."""
    root = tmp / "modules"
    ok = {"id": "good", "name": {"ru": "Хороший", "en": "Good"}, "group": "project", "order": 10,
          "needs": ["project"], "commands": ["agent:ask"], "for": "1.0.0", "dev": True}
    _write(root / "good" / "module.json", ok)
    # нестрогие поля не того типа: раздел поднимается с умолчаниями
    _write(root / "types" / "module.json", {
        "id": "types", "name": {"ru": "Типы"}, "order": "abc", "needs": "project",
        "commands": "agent:ask", "group": 5, "about": 7, "dev": "yes", "for": 3})
    _write(root / "badgroup" / "module.json", {"id": "badgroup", "name": {"ru": "Группа"}, "group": "nope"})
    _write(root / "inf" / "module.json",
           '{"id": "inf", "name": {"ru": "Ин"}, "order": Infinity}')
    # обязательное нарушено: запись {id, error}
    _write(root / "strname" / "module.json", {"id": "strname", "name": "Строка"})
    _write(root / "noru" / "module.json", {"id": "noru", "name": {"en": "Only English"}})
    _write(root / "other" / "module.json", {"id": "elsewhere", "name": {"ru": "Чужой"}})
    _write(root / "future" / "module.json", {"id": "future", "name": {"ru": "Будущий"}, "api_version": 99})
    # api_version не смягчается: не тот тип — ошибка раздела, как и версия из будущего
    for sid, ver in (("verstr", "99"), ("verfloat", 2.0), ("verbool", True), ("verzero", 0), ("vernull", None)):
        _write(root / sid / "module.json", {"id": sid, "name": {"ru": "Версия"}, "api_version": ver})
    # не строка в НЕосновном языке имени: раздел поднимается, этот язык отброшен с пометкой
    _write(root / "nameen" / "module.json", {"id": "nameen", "name": {"ru": "Имя", "en": None}})
    _write(root / "nonamenull" / "module.json", {"id": "nonamenull", "name": {"ru": None}})
    # явный null в необязательных полях — «поля нет»: умолчания без единого предупреждения
    _write(root / "nullfields" / "module.json", {
        "id": "nullfields", "name": {"ru": "Нули"}, "order": None, "needs": None, "commands": None,
        "group": None, "about": None, "dev": None, "for": None})
    _write(root / "junk" / "module.json", "{ не json")
    _write(root / "list" / "module.json", "[1, 2]")
    _write(root / "empty" / "module.json", "")
    ck, restore = _ck_modules_dir(root)
    try:
        got = ck.modules()               # не бросает
    finally:
        restore()
    by = {m["id"]: m for m in got}
    assert set(by) == {"good", "types", "badgroup", "inf", "strname", "noru", "other", "future",
                       "junk", "list", "empty", "verstr", "verfloat", "verbool", "verzero", "vernull",
                       "nameen", "nonamenull", "nullfields"}, sorted(by)
    # валидный на месте и без замечаний
    g = by["good"]
    assert "warn" not in g and "error" not in g and g["dev"] is True and g["order"] == 10
    assert g["needs"] == ["project"] and g["commands"] == ["agent:ask"] and g["for"] == "1.0.0"
    assert g["behind"] is True
    # нестрогое: умолчания и причина в warn
    t = by["types"]
    assert "error" not in t and t["order"] == 100 and t["needs"] == [] and t["commands"] == []
    assert t["group"] == "project" and t["dev"] is False and t["about"] == {}
    assert t["for"] == ck.kit_version() and t["behind"] is False
    warned = "\n".join(t["warn"])
    for field in ("order", "needs", "commands", "group", "about", "dev", "for"):
        assert "«%s»" % field in warned, (field, t["warn"])
    assert by["badgroup"]["group"] == "project" and "nope" in " ".join(by["badgroup"]["warn"])
    assert by["inf"]["order"] == 100 and by["inf"]["warn"], by["inf"]
    # обязательное: только error
    for bad in ("strname", "noru", "other", "future", "junk", "list", "empty",
                "verstr", "verfloat", "verbool", "verzero", "vernull", "nonamenull"):
        assert set(by[bad]) == {"id", "error"} and by[bad]["error"], (bad, by[bad])
    assert "v99" in by["future"]["error"]
    for bad in ("verstr", "verfloat", "verbool", "vernull"):
        assert "api_version" in by[bad]["error"], (bad, by[bad])
    ne = by["nameen"]
    assert "error" not in ne and ne["name"] == {"ru": "Имя"} and len(ne["warn"]) == 1, ne
    nf = by["nullfields"]
    assert "error" not in nf and "warn" not in nf, nf
    assert nf["order"] == 100 and nf["needs"] == [] and nf["group"] == "project" and nf["dev"] is False
    assert "объект" in by["list"]["error"]
    # порядок: сначала группы меню, внутри — order, потом id
    ids = [m["id"] for m in got if "error" not in m]
    assert ids[0] == "good" or by["good"]["order"] <= by[ids[0]]["order"], ids


@test
def test_load_manifest_lenient_drops_only_optional(tmp: Path):
    """`lenient=True`: необязательное поле не того типа отбрасывается с пометкой, обязательное — по-прежнему отказ."""
    import aurora_common as C
    p = _write(tmp / "m" / "module.json", {"id": "m", "name": {"ru": "М"}, "order": "x", "dev": 1})
    strict, rep = C.load_manifest(p, "module")
    assert strict is None and "order" in rep[0], rep
    got, notes = C.load_manifest(p, "module", lenient=True)
    assert got and "order" not in got and "dev" not in got and got["api_version"] == 1, got
    assert len(notes) == 2 and all("отброшено" in n for n in notes), notes
    got, notes = C.load_manifest(_write(tmp / "n" / "module.json", {"id": "n", "name": 5}), "module", lenient=True)
    assert got is None and "name" in notes[0], notes
    clean, notes = C.load_manifest(_write(tmp / "c" / "module.json", {"id": "c", "name": "С"}), "module", lenient=True)
    assert clean and notes == []
    # api_version смягчению не подлежит ни в каком виде: «99», 2.0, true, null — отказ, как и в строгом режиме
    for i, ver in enumerate(("99", 2.0, True, None, [1])):
        p = _write(tmp / ("v%d" % i) / "module.json", {"id": "v%d" % i, "name": "В", "api_version": ver})
        for lenient in (False, True):
            got, notes = C.load_manifest(p, "module", lenient=lenient)
            assert got is None and "api_version" in notes[0], (ver, lenient, got, notes)
    # явный null в необязательном — «поля нет» без записи в отчёт; не строка в языке — отброшен только язык
    got, notes = C.load_manifest(_write(tmp / "z" / "module.json", {
        "id": "z", "name": {"ru": "Я", "en": 5}, "about": {"en": None}, "order": None, "dev": None}),
        "module", lenient=True)
    assert got and "order" not in got and "dev" not in got, got
    assert got["name"] == {"ru": "Я"} and got["about"] == {}, got
    assert len(notes) == 2 and all("языков" in n for n in notes), notes
    strict, notes = C.load_manifest(tmp / "z" / "module.json", "module")
    assert strict is None, notes


@test
def test_cockpit_modules_messages_translate_to_english(tmp: Path):
    """`error` и `warn` из `modules()` по-английски без русских слов: шаблоны есть в `data/en.json`, `warn` — в списках перевода."""
    import re as _re
    root = tmp / "modules"
    _write(root / "types" / "module.json", {
        "id": "types", "name": {"ru": "Т", "en": 3}, "order": "abc", "needs": "project",
        "commands": "agent:ask", "group": 5, "about": 7, "dev": "yes", "for": 3})
    _write(root / "badgroup" / "module.json", {"id": "badgroup", "name": {"ru": "Т"}, "group": "nope"})
    _write(root / "inf" / "module.json", '{"id": "inf", "name": {"ru": "Т"}, "order": Infinity}')
    _write(root / "future" / "module.json", {"id": "future", "name": {"ru": "Т"}, "api_version": 99})
    _write(root / "verstr" / "module.json", {"id": "verstr", "name": {"ru": "Т"}, "api_version": "2"})
    _write(root / "noid" / "module.json", {"name": {"ru": "Т"}})
    _write(root / "badname" / "module.json", {"id": "badname", "name": 5})
    _write(root / "junk" / "module.json", "{ не json")
    _write(root / "list" / "module.json", "[1]")
    _write(root / "other" / "module.json", {"id": "elsewhere", "name": {"ru": "Т"}})
    ck, restore = _ck_modules_dir(root)
    try:
        got = ck.modules()
        ru = [x for m in got for x in ([m["error"]] if "error" in m else []) + m.get("warn", [])]
        en = ck.translate_payload({"modules": got}, "en")["modules"]
    finally:
        restore()
    texts = [x for m in en for x in ([m["error"]] if "error" in m else []) + m.get("warn", [])]
    assert len(texts) == len(ru) >= 14, texts
    # «не json» из файла попадает в сообщение разбора как есть (это текст файла, а не шаблона)
    left = [t for t in texts if _re.search("[а-яА-ЯёЁ]", t.replace("не json", ""))]
    assert not left, left
    assert any("field «order» is not a number" in t for t in texts), texts


def _ck_skins_dir(path):
    """Панель, у которой папка скинов — `path`; вернуть (модуль, откат)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    keep = ck.SKINS_DIR
    ck.SKINS_DIR = str(path)

    def restore():
        ck.SKINS_DIR = keep
    return ck, restore


@test
def test_ext_behind_compares_minor_only(tmp: Path):
    """`ext_behind` — одна функция для скинов и разделов: расходятся по minor — отстало, патч не в счёт."""
    import aurora_common as C
    assert C.ext_behind("1.173.0", "1.173.9") is False
    assert C.ext_behind("1.172.5", "1.173.0") is True
    assert C.ext_behind("1.174.0", "1.173.0") is True          # переходное поведение: «новее» тоже расхождение
    assert C.ext_behind("2", "2.0.1") is True                  # «2» и «2.0» — разные minor по строке, как и раньше
    assert C.ext_api_problem(1, "x") == "" and C.ext_api_problem(C.EXT_API_VERSION + 1, "x")
    for bad in (0, -1, "1", 1.0, True, None):
        assert C.ext_api_problem(bad, "x"), bad


@test
def test_cockpit_skins_survive_bad_files_and_future_api(tmp: Path):
    """`skins()` на папке с кривыми скинами не падает: без `for`, другая minor, `api` из будущего,
    не UTF-8, не файл — каждый отвечает своей строкой; плохой скин не прячет хорошие."""
    root = tmp / "skins"
    head = "/* name: %s\n   %sabout: Описание %s. */\n:root{--primary:#000}\n"
    _write(root / "old.css", head % ("Старый", "", "без версии"))                       # без for и api: сегодняшний, v1
    _write(root / "minor.css", head % ("Другая", "for: 1.1.0\n   ", "версия ядра"))
    _write(root / "new.css", head % ("Новый", "for: 9.9.9\n   api: 1\n   ", "api объявлен"))
    _write(root / "future.css", head % ("Будущий", "api: 2\n   ", "из будущего"))
    _write(root / "zero.css", head % ("Нулевой", "api: 0\n   ", "ноль"))
    _write(root / "word.css", head % ("Словом", "api: x\n   ", "не число"))
    _write(root / "float.css", head % ("Дробный", "api: 1.5\n   ", "не целое"))
    _write(root / "inabout.css", "/* name: В тексте\n   about: см. api: 7 в описании. */\n")   # не в начале строки — не поле
    (root / "latin.css").write_bytes(b"/* name: \xff\xfe\n   about: x */\n")                # не UTF-8
    (root / "dir.css").mkdir()                                                             # не файл: open падает OSError
    (root / "readme.txt").write_text("не скин", encoding="utf-8")
    ck, restore = _ck_skins_dir(root)
    try:
        got = ck.skins()                   # не бросает
        kit = ck.kit_version()
        en = ck.translate_payload({"rejected": [r for r in got if "error" in r]}, "en")["rejected"]
    finally:
        restore()
    by = {r["id"]: r for r in got}
    assert set(by) == {"old", "minor", "new", "future", "zero", "word", "float", "inabout", "latin", "dir"}, sorted(by)
    old = by["old"]
    assert "error" not in old and old["api"] == 1 and old["for"] == kit and old["behind"] is False, old
    assert old["name"] == "Старый" and "без версии" in old["about"], old
    assert by["minor"]["behind"] is True and by["minor"]["for"] == "1.1.0" and "error" not in by["minor"]
    assert by["new"]["behind"] is True and by["new"]["api"] == 1, by["new"]
    assert "error" not in by["inabout"] and by["inabout"]["api"] == 1, by["inabout"]
    for sid in ("future", "zero", "word", "float", "latin", "dir"):
        assert set(by[sid]) == {"id", "error"} and by[sid]["error"], (sid, by[sid])
    assert "v2" in by["future"]["error"] and "обновите кит" in by["future"]["error"], by["future"]
    assert "skin «future»" in by["future"]["error"]
    assert "UTF-8" in by["latin"]["error"], by["latin"]
    # у автора скина поле называется `api`, а не `api_version` (так называют его манифесты)
    for sid in ("zero", "word", "float"):
        assert "api должен быть целым" in by[sid]["error"] and "api_version" not in by[sid]["error"], by[sid]
    # английский: ни одного русского слова в причинах
    import re as _re
    assert len(en) == 6 and not [e for e in en if _re.search("[а-яА-ЯёЁ]", e["error"])], en
    assert any("extension API v2" in e["error"] for e in en), en
    assert any("api must be an integer from 1" in e["error"] for e in en), en


@test
def test_cockpit_skins_of_the_kit_stay_valid(tmp: Path):
    """Поставляемые скины читаются как прежде: без ошибок, api — 1, отставших нет."""
    ck, restore = _ck_skins_dir(Path(__file__).resolve().parents[2] / "cockpit" / "skins")
    try:
        got = ck.skins()
    finally:
        restore()
    assert len(got) >= 5 and not [r for r in got if "error" in r], got
    assert all(r["api"] == 1 and r["for"] and not r["behind"] for r in got), got


@test
def test_kit_i18n_check_reports_bad_manifests_instead_of_crashing(tmp: Path):
    """`kit_i18n --check` на ките с кривым module.json или connector.json: код 1 и строка в отчёте,
    а не traceback; кривой манифест пропущен, соседний годный читается."""
    import importlib
    KI = importlib.import_module("kit_i18n")
    mods = tmp / "modules"
    _write(mods / "good" / "module.json", {"id": "good", "name": {"ru": "Хороший"}})
    _write(mods / "broken" / "module.json", "{ не json")
    _write(mods / "future" / "module.json", {"id": "future", "name": {"ru": "Т"}, "api_version": 99})
    kit = tmp / "kit"
    _write(kit / "connectors" / "ok" / "connector.json", _connector("ok", role_what="Задачи команды"))
    _write(kit / "connectors" / "bad" / "connector.json", "{\"id\": ")
    _write(kit / "connectors" / "nomirror" / "connector.json", {"id": "nomirror", "kind": "board", "run": {}})
    keep = (KI.MODULES, KI.KIT, list(KI.SKIPPED))
    out = io.StringIO()
    try:
        KI.SKIPPED.clear()
        KI.MODULES = str(mods)
        KI.KIT = str(kit)
        ids = KI.module_ids()
        texts = KI.connector_texts()           # на старом коде — JSONDecodeError
        skipped = list(KI.SKIPPED)
        argv, sys.argv = sys.argv, ["kit_i18n.py", "--check"]
        try:
            with contextlib.redirect_stdout(out):
                KI.KIT = keep[1]               # остальное сверяется с настоящим китом
                rc = KI.main()
        finally:
            sys.argv = argv
    finally:
        KI.MODULES, KI.KIT = keep[0], keep[1]
        KI.SKIPPED[:] = keep[2]
    assert ids == ["good"], ids
    assert texts == {"Задачи команды"}, texts
    assert len(skipped) == 4 and any("module «broken»" in s for s in skipped) \
        and any("v99" in s for s in skipped) and any("connector «bad»" in s for s in skipped) \
        and any("«mirror»" in s for s in skipped), skipped
    report = out.getvalue()
    assert rc == 1 and "манифест расширения не принят" in report and "module «broken»" in report, report


@test
def test_skin_with_foreign_byte_after_the_header_still_works(tmp: Path):
    """Посторонний не-UTF-8 байт ДАЛЬШЕ шапки (комментарий в cp1251) скин не отвергает: шапка
    читается, CSS отдаётся. Не-UTF-8 в самой шапке — отказ с причиной."""
    root = tmp / "skins"
    root.mkdir()
    head = "/* name: Рабочий\n   api: 1\n   about: Описание. */\n".encode("utf-8")
    body = ":root{--primary:#123}\n".encode("ascii")
    (root / "winedit.css").write_bytes(head + b"/* " + "комментарий".encode("cp1251") + b" */\n" + body)
    (root / "far.css").write_bytes(head + b"a{}\n" * 3000 + b"/* \xe0\xe1 */\n")      # байт за пределами головы
    (root / "inhead.css").write_bytes(b"/* name: \xe0\xe1\n   api: 1 */\n" + body)       # байт в самой шапке
    ck, restore = _ck_skins_dir(root)
    try:
        by = {r["id"]: r for r in ck.skins()}
        css = ck.skin_css("winedit")
    finally:
        restore()
    for sid in ("winedit", "far"):
        assert "error" not in by[sid] and by[sid]["name"] == "Рабочий" and by[sid]["api"] == 1, by[sid]
    assert "--primary:#123" in css, css
    assert set(by["inhead"]) == {"id", "error"} and "UTF-8" in by["inhead"]["error"], by["inhead"]


SKINS_PANEL_HARNESS = r"""
const toasts = [];
let lang = "ru";
const t = (key, vars) => lang + ":" + key + ":" + JSON.stringify(vars || {});
const toast = (text, kind) => { toasts.push({text, kind}); };
const sel = {innerHTML: "", options: [], append(o){ this.options.push(o); }, onchange: null};
const $ = () => sel;
const el = (tag, attrs, text) => ({tag, attrs, text});
const S = {state: {kit: {version: "1.0.0"}}};
const localStorage = {getItem: () => null, setItem: () => {}};
const applied = [];
async function applySkin(id){ applied.push(id); }
let reply = null;
const api = async () => reply;
@FN@
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
const rejected = [{id: "broken", error: "skin «broken»: поле «api» — не целое число — пропущен"}];

(async () => {
  reply = {skins: [], rejected};
  await loadSkins();
  check(toasts.length === 1, "первый показ: одно предупреждение, а их " + toasts.length);
  check(toasts[0] && toasts[0].kind === "warn", "предупреждение должно быть уровня warn");
  check(toasts[0] && toasts[0].text.includes("skin.broken") && toasts[0].text.includes('"id":"broken"')
        && toasts[0].text.includes("не целое число"), "в тексте нет ключа, id и причины: " + (toasts[0] && toasts[0].text));
  await loadSkins();
  check(toasts.length === 1, "повторное чтение списка с той же причиной не должно показывать её снова");
  lang = "en";                                  // смена языка перечитывает список: причина сервера не меняется
  await loadSkins();
  check(toasts.length === 1, "смена языка не должна показывать ту же причину заново");
  reply = {skins: [], rejected: [{id: "broken", error: "skin «broken»: файл не в кодировке UTF-8 — пропущен"}]};
  await loadSkins();
  check(toasts.length === 2, "новая причина для того же скина — новое предупреждение");
  reply = {skins: [], rejected: [{id: "other", error: "skin «broken»: файл не в кодировке UTF-8 — пропущен"}]};
  await loadSkins();
  check(toasts.length === 3, "та же причина у другого скина — отдельное предупреждение");
  reply = {skins: [{id: "zine", name: "Зин", about: "", "for": "1.0.0", behind: false}]};
  await loadSkins();
  check(toasts.length === 3, "ответ без rejected не показывает ничего");
  check(applied[applied.length - 1] === "zine" && sel.options.length === 1, "список скинов собирается как прежде");
  reply = null;                                 // сервер не ответил: панель не падает и ничего не показывает
  await loadSkins();
  check(toasts.length === 3, "пустой ответ не должен показывать предупреждений");
  console.log(JSON.stringify({fails}));
})().catch(e => { console.log(JSON.stringify({fails: ["исключение: " + (e && e.stack || e)]})); });
"""


@test
def test_panel_shows_why_a_skin_was_rejected(tmp: Path):
    """Причину отказа скина панель показывает (`rejected` из /api/skins): `loadSkins` вызвана под подставным DOM (нужен node,
    иначе проверка пропускается) — предупреждение уровня warn с id и причиной, ровно один раз на пару «id + причина»
    (повторное чтение и смена языка его не повторяют; новая причина или другой скин — показывается). Строки каталога —
    в обоих языках, с подстановками {id} и {why}."""
    kit = Path(__file__).resolve().parents[2]
    for lang in ("ru", "en"):
        cat = json.loads((kit / "cockpit" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "{id}" in cat.get("skin.broken", "") and "{why}" in cat["skin.broken"], (lang, cat.get("skin.broken"))
    node = shutil.which("node")
    if not node:
        return
    panel = (kit / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    a = panel.index("const SKINS_REJECTED_SHOWN")
    fn = panel[a:panel.index("async function applySkin", a)]
    src = tmp / "skins_panel.mjs"
    src.write_text(SKINS_PANEL_HARNESS.replace("@FN@", fn), encoding="utf-8")
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert result["fails"] == [], "\n".join(result["fails"])


@test
def test_loader_has_no_dead_schemas(tmp: Path):
    """Схемы без читателя в боевом коде не живут: у скина шапку разбирает панель, у провайдера Git
    — `git_sync`; загрузчик такие виды не знает и отвечает причиной. У вида `profile` читатель
    боевой: `cockpit.profiles()` (профиль рабочего места, BL-ARC-04)."""
    import aurora_common as C
    assert set(C.EXT_SCHEMAS) == {"connector", "module", "profile"}, sorted(C.EXT_SCHEMAS)
    for kind in ("skin", "provider"):
        got, rep = C.load_manifest(_write(tmp / "x.json", {"id": "x"}), kind)
        assert got is None and "неизвестен" in rep[0], (kind, rep)


@test
def test_connector_mirror_path_must_stay_inside_the_project(tmp: Path):
    """`mirror.default_path`: простой относительный путь; абсолютный, `..`, диск, перевод строки,
    служебные для YAML знаки — манифест пропущен с причиной (иначе install создаёт папку вне проекта,
    а панель пишет путь в конфиг как есть)."""
    import aurora_common as C
    for good in ("Sources/Notion", "Sources/Notion/", "Sources/Мой источник", "Notion", "a/b-c/d_e.f"):
        got, rep = C.load_connector_manifest(_write(tmp / "g.json", _connector(
            "demo", mirror={"default_path": good})))
        assert got and not rep, (good, rep)
    for bad in ("/etc/x", "\\\\srv\\x", "\\x", "C:/x", "C:\\x", "../x", "a/../x", "a/..", "a\\..\\b",
                "a/./b", "a//b", "Sources/x\ny: z", "Sources/x\n", "a\x00b", "a\tb", "x #c", "x: y", "'x'",
                " Sources/x", "Sources/x ", "~/x", "."):
        got, rep = C.load_connector_manifest(_write(tmp / "b.json", _connector(
            "demo", mirror={"default_path": bad})))
        assert got is None and "mirror.default_path" in rep[0], (bad, got, rep)
    # без пути зеркала — по-прежнему годный манифест (зеркало берётся из конфига проекта)
    assert C.load_connector_manifest(_write(tmp / "n.json", _connector("demo", mirror={"state": "s.md"})))[0]
    assert C.load_connector_manifest(_write(tmp / "e.json", _connector("demo", mirror={"default_path": ""})))[0]


@test
def test_install_skips_connector_with_escaping_mirror(tmp: Path):
    """Манифест с `mirror.default_path` за пределами проекта install пропускает строкой в отчёте:
    папка вне проекта не создаётся, соседний годный модуль ставится."""
    import install_aurora as IA
    kit = tmp / "kit"
    for cid, path in (("evil", "../outside_dir"), ("abs", str(tmp / "abs_dir")), ("fine", "Sources/Fine")):
        _write(kit / "connectors" / cid / "connector.json", _connector(cid, mirror={"default_path": path}))
    target = tmp / "installed"
    target.mkdir()
    keep = IA.KIT_ROOT
    IA.KIT_ROOT = kit
    try:
        inst = IA.Installer(target, "Demo", "Demo", None, None, force=False, dry_run=False)
        with contextlib.redirect_stdout(io.StringIO()):
            inst.install_connectors()
    finally:
        IA.KIT_ROOT = keep
    log = "\n".join(inst.actions)
    assert log.count("skip (манифест не принят)") == 2 and "mirror.default_path" in log, log
    assert not (tmp / "outside_dir").exists() and not (tmp / "abs_dir").exists(), "папка вне проекта создана"
    assert (target / "Sources" / "Fine").is_dir() and (target / ".aurora" / "connectors" / "fine.json").is_file()
    assert not (target / ".aurora" / "connectors" / "evil.json").exists()


@test
def test_panel_does_not_write_unsafe_mirror_path_into_config(tmp: Path):
    """Подключение модуля из панели: небезопасный или отсутствующий `mirror.default_path` установленного
    манифеста не попадает в `aurora.config.yaml`, ответ — ошибка, а не KeyError; годный модуль пишется."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    root = tmp / "proj"
    cfg = root / "aurora.config.yaml"
    _write(cfg, "name: Demo\nsources:\n  - id: Old\n")
    for cid, mirror in (("evil", {"default_path": "../x\nrole: y"}), ("nopath", {"state": "s.md"}),
                        ("fine", {"default_path": "Sources/Fine"})):
        _write(root / ".aurora" / "connectors" / f"{cid}.json", _connector(cid, mirror=mirror))
    before = cfg.read_text(encoding="utf-8")
    for bad in ("evil", "nopath"):
        res = ck.Handler._write_sources(None, str(root), ["fine", bad])
        assert "error" in res and bad in res["error"], (bad, res)
        assert cfg.read_text(encoding="utf-8") == before, "конфиг изменён при отказе"
    res = ck.Handler._write_sources(None, str(root), ["fine"])
    assert res.get("ok") and "path: Sources/Fine" in cfg.read_text(encoding="utf-8"), res


def _project_with_hostile_manifests(tmp: Path) -> Path:
    """Проект, в `.aurora/connectors/` которого уже лежат манифесты с путями зеркала за пределами проекта
    (скопированы прежним выпуском или положены вручную) — `install` их уже не увидит, а реестр читает."""
    root = make_project(tmp)
    for cid, path in (("evil", "../outside_dir"), ("inject", "Sources/x\nrole: y"), ("fine", "Sources/Fine")):
        _write(root / ".aurora" / "connectors" / f"{cid}.json", _connector(
            cid, mirror={"default_path": path, "legacy_path_key": f"{cid}_dir"}))
    return root


@test
def test_registry_drops_connector_with_escaping_mirror_path(tmp: Path):
    """Проверка пути зеркала стоит в общем читателе проектных манифестов: `sources_registry.load()` отбрасывает манифест
    с небезопасным `mirror.default_path` строкой в отчёт реестра, годный читается; путь из `sources:` конфига
    белым списком не ограничивается: его пишет владелец, `instances()` отдаёт его как есть."""
    import sources_registry as R
    root = _project_with_hostile_manifests(tmp)
    mods = R.installed(str(root))
    assert "fine" in mods and "evil" not in mods and "inject" not in mods, sorted(mods)
    assert R.load(str(root / ".aurora" / "connectors" / "evil.json")) == {}
    rep = "\n".join(R.registry_report())
    assert "evil" in rep and "inject" in rep and "mirror.default_path" in rep, rep
    assert all("\n" not in line for line in R.registry_report()), rep      # строка отчёта — одна строка
    paths = [i["path"] for i in R.instances(str(root))]
    assert "Sources/Fine" in paths and not any(".." in p or "\n" in p for p in paths), paths
    # путь из конфига пишет сам владелец: реестр отдаёт его как есть, без белого списка манифеста
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "sources:\n  - id: Esc\n    module: fine\n    path: ../esc\n"
                   "  - id: Good\n    module: fine\n    path: Sources/Good\n", encoding="utf-8")
    got = {i["id"]: i["path"] for i in R.instances(str(root))}
    assert got == {"Esc": "../esc", "Good": "Sources/Good"}, got
    # годные пути по-прежнему принимаются (в том числе с хвостовым «/»)
    assert R.safe_relpath("Sources/Notion/") and not R.safe_relpath("/etc") and not R.safe_relpath("")


@test
def test_settings_form_and_update_do_not_use_escaping_mirror_path(tmp: Path):
    """Манифест с `../outside_dir` и «\\nrole: y» лежит в `.aurora/connectors/`: форма настроек (`source_sections`) не
    пишет эти строки в конфиг, `missing_dirs` их не называет, а `update --structure-only --apply` не создаёт ничего вне
    проекта (и в проекте — ничего с такими именами)."""
    import aurora_setup as S
    import aurora_update as AU
    root = _project_with_hostile_manifests(tmp)
    src, _skills = S.source_sections(root, "demo")
    assert "outside_dir" not in src and "role: y" not in src and "Sources/Fine" in src, src
    miss = AU.missing_dirs(root)
    assert not any(".." in d or "\n" in d for d in miss), miss
    assert "Sources/Fine" in AU.mirror_dirs(root), AU.mirror_dirs(root)
    before = sorted(p.name for p in tmp.iterdir())
    cp = run("aurora_update.py", str(root), "--structure-only", "--apply", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert sorted(p.name for p in tmp.iterdir()) == before and not (tmp / "outside_dir").exists(), \
        "update создал папку вне проекта"
    assert (root / "Sources" / "Fine").is_dir() and not (root / "Sources" / "x").exists()
    assert not any("outside_dir" in str(p) or "role: y" in str(p) for p in root.rglob("*")), "в проекте появился чужой путь"


# пути, которые владелец пишет в `sources:` собственного конфига: скобки, плюс, обратная косая, фигурные скобки, «..»
_OWNER_PATHS = {"Old": "Sources/Wiki (old)", "Prj": "Sources/JIRA (PRJ-A)", "Plus": "Sources/Wiki+Docs",
                "Shared": "../Shared/Confluence", "Curly": "Sources/Wiki {old}"}


def _project_with_owner_paths(tmp: Path) -> Path:
    """Проект, чей `sources:` ссылается на установленные модули с путями вне белого списка манифеста."""
    root = make_project(tmp)
    rows = "".join(f"  - id: {k}\n    module: {'jira-dc' if k == 'Prj' else 'confluence-dc'}\n    path: {v}\n"
                   for k, v in _OWNER_PATHS.items())
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + "\n\nsources:\n" + rows, encoding="utf-8")
    return root


@test
def test_owner_mirror_paths_in_config_are_kept(tmp: Path):
    """Путь зеркала из `sources:` собственного конфига («Sources/Wiki (old)», «Sources/JIRA (PRJ-A)», «../Shared/…»)
    остаётся в `instances()`, `roles()`, `mirror_paths()`; `update` не создаёт папок вне проекта (`mirror_dirs()`); белый список ограничивает только путь
    из манифеста."""
    import aurora_update as AU
    import sources_registry as R
    root = _project_with_owner_paths(tmp)
    got = {i["id"]: i["path"] for i in R.instances(str(root))}
    assert got == _OWNER_PATHS, got
    roles = R.roles(str(root))
    assert roles.get("Sources/Wiki (old)") == "artifacts" and roles.get("Sources/JIRA (PRJ-A)") == "tasks", roles
    assert set(R.mirror_paths(str(root))) == set(_OWNER_PATHS.values()), R.mirror_paths(str(root))
    inside = set(_OWNER_PATHS.values()) - {"../Shared/Confluence"}
    assert set(AU.mirror_dirs(root)) == inside, AU.mirror_dirs(root)            # папки, которые update вправе создать
    assert AU.outside_mirror_dirs(root) == ["../Shared/Confluence"], AU.outside_mirror_dirs(root)   # значение не правится
    assert not any("Sources/Wiki" in line or "Shared" in line for line in R.registry_report()), R.registry_report()


@test
def test_settings_form_keeps_owner_mirror_path(tmp: Path):
    """Первое же сохранение формы настроек не стирает зеркало с путём владельца: запись `sources:` на месте
    вместе со строкой sync-скилла, повторное сохранение конфиг не меняет."""
    import aurora_setup as S
    root = _project_with_owner_paths(tmp)
    src, skills = S.source_sections(root, "demo")
    for k, v in _OWNER_PATHS.items():
        assert f"id: {k}\n    module:" in src and f"path: {v}\n" in src, (k, src)
    assert "jira-export-demo" in skills or "confluence-sync-demo" in skills, skills
    cfg = root / "aurora.config.yaml"
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {"name": "Новое имя"})
    text = cfg.read_text(encoding="utf-8")
    assert 'name: "Новое имя"' in text, text
    for v in _OWNER_PATHS.values():
        assert f"path: {v}\n" in text, (v, text)
    import sources_registry as R
    assert {i["path"] for i in R.instances(str(root))} == set(_OWNER_PATHS.values())
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {})
    assert cfg.read_text(encoding="utf-8") == text, "повторное сохранение формы меняет конфиг"


@test
def test_panel_sources_survives_brace_in_registry_output(tmp: Path):
    """Реестр печатает JSON в stdout, заметки — в stderr; панельный `sources()` не режет вывод по последней «}»:
    «}» в пути из конфига и в тексте заметки (манифест с неизвестной ролью) раздел «Источники» не опустошает."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    root = _project_with_owner_paths(tmp)
    _write(root / ".aurora" / "connectors" / "weird.json", _connector("weird", role="bad}role"))
    cp = run("sources_registry.py", "--json", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "bad}role" in cp.stderr and "bad}role" not in cp.stdout, cp.stderr     # заметка — в stderr, не в stdout
    assert json.loads(cp.stdout)["instances"], cp.stdout                           # stdout — чистый JSON
    res = ck.sources(str(root))
    assert "error" not in res, res
    assert {i["path"] for i in res["instances"]} == set(_OWNER_PATHS.values()), res["instances"]


def _panel():
    """Модуль панели (`aurora_cockpit`): путь к нему добавляется один раз."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cockpit"))
    import importlib
    return importlib.import_module("aurora_cockpit")


_OWNER_SOURCES = ("# Подключённые модули источников.\nsources:\n"
                  "  - id: Old\n    module: confluence-dc\n    path: Sources/Wiki (old)\n"
                  "  - id: Jira\n    module: jira-dc\n    path: Sources/JIRA (PRJ-A)\n"
                  "  - id: Second\n    module: confluence-dc\n    path: Sources/Wiki+Docs\n    note: \"свой ключ\"\n")


@test
def test_panel_save_choice_keeps_owner_sources(tmp: Path):
    """«Сохранить выбор» в «Зеркалах» не пересобирает `sources:` из манифестов: с неизменёнными галочками конфиг
    остаётся теми же байтами (id и путь владельца, второй экземпляр модуля, его лишние ключи); отмеченный новый
    модуль добавляется с путём манифеста, старые записи не трогает; снятый модуль убирает только свои экземпляры."""
    ck = _panel()
    root = make_project(tmp)
    _write(root / ".aurora" / "connectors" / "demo.json", _connector("demo"))
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + _OWNER_SOURCES, encoding="utf-8")
    before = cfg.read_bytes()
    res = ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc"])
    assert res.get("ok"), res
    assert cfg.read_bytes() == before, cfg.read_text(encoding="utf-8")
    # новый модуль: путь манифеста только у него, записи владельца дословно на месте
    res = ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc", "demo"])
    assert res.get("ok"), res
    text = cfg.read_text(encoding="utf-8")
    assert "id: Old\n    module: confluence-dc\n    path: Sources/Wiki (old)\n" in text, text
    assert "id: Second\n    module: confluence-dc\n    path: Sources/Wiki+Docs\n    note: \"свой ключ\"\n" in text, text
    assert "id: demo\n    module: demo\n    path: Sources/demo\n" in text, text
    import sources_registry as R
    assert {i["id"]: i["path"] for i in R.instances(str(root))} == {
        "Old": "Sources/Wiki (old)", "Jira": "Sources/JIRA (PRJ-A)", "Second": "Sources/Wiki+Docs",
        "demo": "Sources/demo"}
    # снят один модуль — уходят ВСЕ его экземпляры, остальные на месте
    res = ck.Handler._write_sources(None, str(root), ["jira-dc", "demo"])
    assert res.get("ok"), res
    got = {i["id"]: i["path"] for i in R.instances(str(root))}
    assert got == {"Jira": "Sources/JIRA (PRJ-A)", "demo": "Sources/demo"}, got


_NESTED_SOURCES = ("sources:\n"
                   "  - id: Old\n    module: confluence-dc\n    path: Sources/Wiki (old)\n"
                   "    exclude:\n      - drafts\n      - tmp\n"
                   "    note: |\n      первая строка\n      - вторая со штрихом\n"
                   "  - id: Jira\n    module: jira-dc\n    path: Sources/JIRA\n")


@test
def test_panel_save_choice_keeps_nested_lists_in_owner_entries(tmp: Path):
    """Запись владельца с вложенным блочным списком и многострочным значением переживает любую перезапись `sources:`
    дословно: «- drafts» под `exclude:` не становится отдельной записью и не теряется при добавлении и снятии модуля."""
    ck = _panel()
    root = make_project(tmp)
    _write(root / ".aurora" / "connectors" / "demo.json", _connector("demo"))
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + _NESTED_SOURCES, encoding="utf-8")
    old = _NESTED_SOURCES.split("  - id: Jira")[0].split("sources:\n")[1]
    import sources_registry as R
    assert [m for m, _i, _p, _r in R.declared_chunks(cfg.read_text(encoding="utf-8"))] == ["confluence-dc", "jira-dc"]
    res = ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc", "demo"])
    assert res.get("ok"), res
    assert old in cfg.read_text(encoding="utf-8"), cfg.read_text(encoding="utf-8")
    res = ck.Handler._write_sources(None, str(root), ["confluence-dc", "demo"])
    assert res.get("ok"), res
    text = cfg.read_text(encoding="utf-8")
    assert old in text and "module: jira-dc" not in text, text


@test
def test_empty_sources_header_means_nothing_connected(tmp: Path):
    """Пустой заголовок `sources:` — «не подключено ничего»: реестр не возвращается к `paths:` (на старом коде
    исторические зеркала оживали); снятие последнего модуля в панели пишет явный пустой заголовок, повторное
    сохранение того же набора конфиг не трогает; секции нет вовсе — по-прежнему два исторических зеркала."""
    ck = _panel()
    import sources_registry as R
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    base = base[:base.index("sources:")] if "sources:" in base else base.rstrip("\n") + "\n\n"
    cfg.write_text(base + "paths:\n  sources_confluence: Sources/Wiki (old)\n", encoding="utf-8")
    # секции нет вовсе: исторические зеркала, путь владельца из paths:
    assert not R.has_section(cfg.read_text(encoding="utf-8"))
    assert {i["module"]: i["path"] for i in R.instances(str(root))}.get("confluence-dc") == "Sources/Wiki (old)"
    assert len(R.instances(str(root))) == 2
    # первое сохранение переносит зеркала в секцию; потом снимаем их все
    res = ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc"])
    assert res.get("ok"), res
    assert {i["module"]: i["path"] for i in R.instances(str(root))}.get("confluence-dc") == "Sources/Wiki (old)"
    res = ck.Handler._write_sources(None, str(root), ["jira-dc"])
    assert res.get("ok"), res
    res = ck.Handler._write_sources(None, str(root), [])
    assert res.get("ok"), res
    text = cfg.read_text(encoding="utf-8")
    assert R.has_section(text) and R.declared_chunks(text) == [], text
    assert "# Подключённые модули источников" in text and "module:" not in text.split("sources:")[1].split("\n\n")[0], text
    # заголовок пуст: реестр не оживляет зеркала из paths: (путь у владельца в paths: остался)
    assert "sources_confluence: Sources/Wiki (old)" in text
    assert R.instances(str(root)) == [] and R.mirror_paths(str(root)) == {} and R.roles(str(root)) == {}
    # повторное сохранение пустого набора конфиг не трогает
    before = cfg.read_bytes()
    assert ck.Handler._write_sources(None, str(root), []).get("ok")
    assert cfg.read_bytes() == before
    # CLI реестра на таком проекте не падает и не показывает зеркал
    out = run("sources_registry.py", "--json", cwd=root).stdout
    assert json.loads(out)["instances"] == []
    # снова отмечаем модуль: он подключается с путём манифеста, а не из paths:
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc"]).get("ok")
    got = {i["module"]: i["path"] for i in R.instances(str(root))}
    assert got == {"confluence-dc": "Sources/Confluence"}, got


@test
def test_settings_form_keeps_empty_sources_header_empty(tmp: Path):
    """Форма настроек поверх пустого заголовка `sources:` оставляет его пустым и не воскрешает зеркала из `paths:`
    (на старом коде секция пропадала); запись без `module:` не подключает исторические зеркала."""
    import aurora_setup as S
    import sources_registry as R
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    base = base[:base.index("sources:")] if "sources:" in base else base.rstrip("\n") + "\n\n"
    cfg.write_text(base + "sources:\natlassian:\n  url: https://example.com\n", encoding="utf-8")
    src, skills = S.source_sections(root, "demo")
    assert src.rstrip("\n").endswith("sources:") and skills == "", (src, skills)
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {"name": "Имя"})
    text = cfg.read_text(encoding="utf-8")
    assert R.has_section(text) and R.declared_chunks(text) == [], text
    assert R.instances(str(root)) == [] and R.mirror_paths(str(root)) == {}
    once = cfg.read_bytes()
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {})
    assert cfg.read_bytes() == once, "повторное сохранение формы меняет конфиг"


@test
def test_panel_save_choice_keeps_owner_path_from_paths_section(tmp: Path):
    """Секции `sources:` ещё нет, зеркало Confluence — по пути владельца в `paths.sources_confluence`: сохранение
    выбора записывает в `sources:` ЭТОТ путь, а не умолчание манифеста; путь манифеста — только новому модулю."""
    ck = _panel()
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "paths:\n  sources_confluence: Sources/Wiki (old)\n",
                   encoding="utf-8")
    res = ck.Handler._write_sources(None, str(root), ["confluence-dc"])
    assert res.get("ok"), res
    text = cfg.read_text(encoding="utf-8")
    assert "module: confluence-dc\n    path: Sources/Wiki (old)\n" in text and "path: Sources/Confluence" not in text, text
    assert "paths:\n  sources_confluence: Sources/Wiki (old)\n" in text, text


@test
def test_settings_form_keeps_owner_paths_keys_in_step_with_sources(tmp: Path):
    """Сохранение формы настроек не возвращает `paths.sources_confluence` / `sources_jira` к умолчанию: путь владельца
    остаётся в `paths:` и совпадает с `sources:`, а выгрузчики читают тот же путь; без ключа пишется умолчание;
    повторное сохранение конфиг не меняет."""
    import os
    import aurora_setup as S
    import confluence_export as CE
    import jira_export as JE
    import sources_registry as R
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "paths:\n  knowledge_db: AuroraKnowledgeDB\n"
                   "  sources_confluence: Sources/Wiki (old)\n  sources_jira: \"Sources/JIRA (PRJ-A)\"\n",
                   encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {"name": "X"})
    text = cfg.read_text(encoding="utf-8")
    assert "  sources_confluence: Sources/Wiki (old)\n" in text, text
    assert "  sources_jira: Sources/JIRA (PRJ-A)\n" in text, text
    got = {i["module"]: i["path"] for i in R.instances(str(root))}
    assert got == {"confluence-dc": "Sources/Wiki (old)", "jira-dc": "Sources/JIRA (PRJ-A)"}, got
    assert CE.read_config(str(root))["out"] == "Sources/Wiki (old)"
    here = os.getcwd()
    os.chdir(root)
    try:
        assert JE.read_config()["out"] == "Sources/JIRA (PRJ-A)"
    finally:
        os.chdir(here)
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {})
    assert cfg.read_text(encoding="utf-8") == text, "повторное сохранение формы меняет конфиг"
    # ключей нет — умолчания, как раньше
    (tmp / "fresh").mkdir()
    fresh = make_project(tmp / "fresh")
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(fresh, {"name": "Y"})
    ftext = (fresh / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "  sources_confluence: Sources/Confluence\n  sources_jira: Sources/JIRA\n" in ftext, ftext


_SOURCES_TEXT = (
    "name: X\n"
    "sources:\n"
    "  # подключённые модули\n"
    "\n"
    "  - exclude:\n"
    "      - drafts\n"
    "      - id: not-an-entry\n"
    "    id: Conf          # комментарий после значения\n"
    "    module: confluence-dc\n"
    "    path: \"Sources/Wiki (old)\"\n"
    "\n"
    "  # запись без module: — реестр её не читает, писатели не теряют\n"
    "  - id: NoModule\n"
    "    note: hand-made\n"
    "  - module: jira-dc\n"
    "    path: Sources/JIRA\n"
    "atlassian:\n"
    "  url: https://example.com\n"
)


@test
def test_sources_registry_one_parser_for_the_section(tmp: Path):
    """Секцию `sources:` разбирает один разбор: вложенный список раньше module:/path: не крадёт id,
    комментарии и запись без module: остаются сырым текстом, склейка возвращает тело байт в байт."""
    import re
    import sources_registry as R
    head, chunks = R.split_section(_SOURCES_TEXT)
    assert R.declared_chunks(_SOURCES_TEXT) == chunks
    assert [(m, i, p) for m, i, p, _r in chunks] == [
        ("confluence-dc", "Conf", "Sources/Wiki (old)"),
        (None, "NoModule", None),
        ("jira-dc", None, "Sources/JIRA"),
    ], chunks
    body = re.search(r"^sources:\n(.*?)(?=^\S)", _SOURCES_TEXT, re.M | re.S).group(1)
    assert head == "  # подключённые модули\n\n", repr(head)
    assert head + "".join(r for _m, _i, _p, r in chunks) == body, "склейка не равна телу секции"
    assert "note: hand-made" in chunks[1][3] and "- drafts" in chunks[0][3], chunks
    assert "# запись без module:" in chunks[0][3], "комментарий между записями потерян"
    # реестр читает ту же секцию тем же разбором: запись с вложенным списком раньше module: даёт id
    (tmp / "aurora.config.yaml").write_text(_SOURCES_TEXT, encoding="utf-8")
    assert R.declared(str(tmp)) == [
        {"id": "Conf", "module": "confluence-dc", "path": "Sources/Wiki (old)"},
        {"module": "jira-dc", "path": "Sources/JIRA"},
    ], R.declared(str(tmp))


@test
def test_sources_registry_section_header_shapes(tmp: Path):
    """Заголовок: пустая секция отличается от отсутствующей; `sources: x` и вложенный ключ — не секция."""
    import sources_registry as R
    assert R.has_section("a: 1\nsources:\natlassian:\n") and R.split_section("sources:\natlassian:\n") == ("", [])
    assert R.has_section("sources:") and R.has_section("sources:  \r\n")
    assert not R.has_section("a: 1\n") and not R.has_section("x:\n  sources:\n    - module: m\n")
    assert not R.has_section("sources: []\n") and R.declared_chunks("sources: []\n") == []
    # запись «-» в отдельной строке: ключи начинаются со следующей значимой строки
    got = R.declared_chunks("sources:\n  -\n    # c\n    module: m\n    id: I\n")
    assert [(m, i) for m, i, _p, _r in got] == [("m", "I")], got
    # отступ записей: «- » глубже первой — вложение, левее или равный — новая запись
    two = R.declared_chunks("sources:\n    - module: a\n      x:\n        - module: inner\n    - module: b\n")
    assert [m for m, _i, _p, _r in two] == ["a", "b"], two


_PANEL_MIXED = ("sources:\n"
                "  # голова секции\n"
                "  - exclude:\n      - drafts\n    id: Conf\n    module: confluence-dc\n    path: Sources/Wiki (old)\n"
                "  # записка между записями\n"
                "  - id: NoModule\n    path: Sources/Hand\n    note: hand-made\n"
                "  - id: Jira\n    module: jira-dc\n    path: Sources/JIRA\n"
                "atlassian:\n  url: https://example.com\n")


@test
def test_panel_save_choice_keeps_entry_without_module_and_comments(tmp: Path):
    """Панель пишет `sources:` единым разбором реестра: запись без `module:` (реестр её не подключает) и комментарии
    между записями и над первой переживают и «ничего не менять», и снятие, и добавление модуля — байт в байт; запись
    с вложенным списком раньше `module:` панель и реестр читают одинаково (на старом коде запись без module: терялась)."""
    ck = _panel()
    root = make_project(tmp)
    _write(root / ".aurora" / "connectors" / "demo.json", _connector("demo"))
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    base = base[:base.index("sources:")] if "sources:" in base else base.rstrip("\n") + "\n\n"
    cfg.write_text(base + _PANEL_MIXED, encoding="utf-8")
    import sources_registry as R
    # одни и те же модули у реестра и у панели (id записи с вложенным списком раньше module: не теряется)
    assert {(i["id"], i["module"]) for i in R.declared(str(root))} == {("Conf", "confluence-dc"), ("Jira", "jira-dc")}
    assert [(m, i) for m, i, _p, _r in R.declared_chunks(cfg.read_text(encoding="utf-8"))] == [
        ("confluence-dc", "Conf"), (None, "NoModule"), ("jira-dc", "Jira")]
    before = cfg.read_bytes()
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc"]).get("ok")
    assert cfg.read_bytes() == before, cfg.read_text(encoding="utf-8")
    # снят jira-dc: уходит только его запись; запись без module:, комментарии, соседи и `atlassian:` на месте
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc"]).get("ok")
    text = cfg.read_text(encoding="utf-8")
    keep = _PANEL_MIXED.replace("  - id: Jira\n    module: jira-dc\n    path: Sources/JIRA\n", "")
    assert text.endswith(keep), text
    assert "  # голова секции\n  - exclude:" in text and "  # записка между записями\n  - id: NoModule" in text, text
    # добавлен новый модуль: записи владельца дословно, новая — в конце секции
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc", "demo"]).get("ok")
    text = cfg.read_text(encoding="utf-8")
    assert "  - id: NoModule\n    path: Sources/Hand\n    note: hand-made\n" in text, text
    assert "  - id: demo\n    module: demo\n    path: Sources/demo\natlassian:" in text, text
    assert {i["id"] for i in R.declared(str(root))} == {"Conf", "demo"}
    # снять все модули: запись без module: переживает и это
    assert ck.Handler._write_sources(None, str(root), []).get("ok")
    text = cfg.read_text(encoding="utf-8")
    assert "note: hand-made" in text and "module: confluence-dc" not in text and "module: demo" not in text, text


@test
def test_sources_section_ends_where_the_yaml_list_ends(tmp: Path):
    """Границы секции `sources:` решает одна функция реестра: список без сдвига (`- id: …` на нулевом отступе, стиль
    PyYAML) и комментарий на нулевом отступе между записями остаются в секции, а комментарий перед следующим ключом
    — нет. Панель пишет по тем же границам: ни одна запись не остаётся «висеть» после перезаписи секции, комментарий
    следующего ключа цел (на старом коде такой список читался как пустая секция: зеркала молча «не подключены»)."""
    ck = _panel()
    import sources_registry as R
    flat = ("project:\n  name: X\nsources:\n- id: Conf\n  module: confluence-dc\n  path: Sources/Conf\n"
            "- id: Jira\n  module: jira-dc\n  path: Sources/JIRA\nartifacts:\n  us: {}\n")
    note = ("sources:\n  - id: Conf\n    module: confluence-dc\n    path: Sources/Conf\n# --- убрано пока\n"
            "  - id: Jira\n    module: jira-dc\n    path: Sources/JIRA\n\n# про artifacts\nartifacts:\n  us: {}\n")
    for text, tail in ((flat, "artifacts:\n  us: {}\n"), (note, "# про artifacts\nartifacts:\n  us: {}\n")):
        span = R.section_span(text)
        assert span and text[span[2]:].startswith(tail), (span, text[span[2]:])
        head, chunks = R.split_section(text)
        assert head + "".join(c[3] for c in chunks) == text[span[1]:span[2]]
        assert [(m, i) for m, i, _p, _r in R.declared_chunks(text)] == [("confluence-dc", "Conf"), ("jira-dc", "Jira")]
    assert R.section_span("project:\n  name: X\n") is None
    # панель: записи под нулевым отступом переписываются целиком, соседний ключ и его комментарий не задеты
    root = make_project(tmp)
    _write(root / ".aurora" / "connectors" / "demo.json", _connector("demo"))
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    base = base[:base.index("sources:")] if "sources:" in base else base.rstrip("\n") + "\n\n"
    cfg.write_text(base + note, encoding="utf-8")
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc"]).get("ok")
    assert cfg.read_text(encoding="utf-8").endswith(note), cfg.read_text(encoding="utf-8")     # ничего не менялось
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc", "demo"]).get("ok")
    text = cfg.read_text(encoding="utf-8")
    assert "# --- убрано пока\n  - id: Jira" in text and text.endswith("\n\n# про artifacts\nartifacts:\n  us: {}\n") \
        or text.endswith("# про artifacts\nartifacts:\n  us: {}\n"), text
    assert text.count("module: jira-dc") == 1 and text.count("module: confluence-dc") == 1, text
    assert {i["id"] for i in R.declared(str(root))} == {"Conf", "Jira", "demo"}
    cfg.write_text(base + flat, encoding="utf-8")                       # список без сдвига: снятие модуля не оставляет хвост
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc"]).get("ok")
    text = cfg.read_text(encoding="utf-8")
    assert "jira-dc" not in text and text.endswith("artifacts:\n  us: {}\n"), text
    assert {i["id"] for i in R.declared(str(root))} == {"Conf"}


@test
def test_settings_form_keeps_source_entries_verbatim(tmp: Path):
    """Форма настроек берёт записи `sources:` сырым текстом: лишние ключи, комментарий и вложенный список раньше
    `module:`, запись без `module:` и комментарии между записями переживают сохранение байт в байт, повторное
    сохранение ничего не меняет; реестр, панель и форма видят один и тот же набор модулей (на старом коде форма
    пересобирала запись как id/module/path и выбрасывала запись без module:)."""
    import aurora_setup as S
    import sources_registry as R
    ck = _panel()
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    base = base[:base.index("sources:")] if "sources:" in base else base.rstrip("\n") + "\n\n"
    cfg.write_text(base + _PANEL_MIXED, encoding="utf-8")
    section = _PANEL_MIXED[:_PANEL_MIXED.index("atlassian:")]
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {"name": "Новое имя"})
    text = cfg.read_text(encoding="utf-8")
    assert 'name: "Новое имя"' in text, text
    assert section in text, text
    assert text.count("sources:\n") == 1, text
    # те же модули у реестра, панели и формы; «ничего не менять» в панели конфиг не трогает
    assert {(i["id"], i["module"]) for i in R.declared(str(root))} == {("Conf", "confluence-dc"), ("Jira", "jira-dc")}
    src, skills = S.source_sections(root, "demo")
    assert [m for m, _i, _p, _r in R.declared_chunks(src)] == ["confluence-dc", None, "jira-dc"], src
    assert "confluence-sync-demo" in skills and "jira-export-demo" in skills, skills
    before = cfg.read_bytes()
    assert ck.Handler._write_sources(None, str(root), ["confluence-dc", "jira-dc"]).get("ok")
    assert cfg.read_bytes() == before
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {})
    assert cfg.read_bytes() == before, "повторное сохранение формы меняет конфиг"
    # хвостовая пустая строка записи не копится от сохранения к сохранению
    cfg.write_text(base + section + "\natlassian:\n  jira:\n    project_key: T\n", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        S.run_answers(root, {})
        once = cfg.read_bytes()
        S.run_answers(root, {})
    assert cfg.read_bytes() == once, "пустые строки между секциями копятся"


@test
def test_settings_form_first_setup_still_writes_generated_mirrors(tmp: Path):
    """Секции `sources:` в конфиге нет (первая настройка, исторические зеркала из `paths:`) — форма пишет прежние две
    записи id/module/path и sync-скиллы; запись без `module:` не мешает генерации записи для модуля, которого в
    секции нет."""
    import aurora_setup as S
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    cfg.write_text(base[:base.index("sources:")] if "sources:" in base else base, encoding="utf-8")
    src, skills = S.source_sections(root, "demo")
    assert "  - id: Confluence\n    module: confluence-dc\n    path: Sources/Confluence\n" in src, src
    assert "  - id: JIRA\n    module: jira-dc\n    path: Sources/JIRA\n" in src, src
    assert "confluence-sync-demo" in skills and "jira-export-demo" in skills, skills
    # в секции только запись без module: — она остаётся, секция есть, поэтому зеркала из paths: не оживают
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + "\n\nsources:\n  - id: Hand\n    note: x\n",
                   encoding="utf-8")
    src, skills = S.source_sections(root, "demo")
    assert src.endswith("sources:\n  - id: Hand\n    note: x\n") and skills == "", src


@test
def test_update_does_not_create_mirror_dirs_outside_project(tmp: Path):
    """Конфиг владельца называет зеркала `../outside` и абсолютный путь: `update --apply` (и `--structure-only`) не
    создаёт ни одной папки вне проекта, называет эти пути строкой отчёта, а обычные пути внутри проекта (в том числе
    «Sources/Wiki (old)») создаёт как раньше. Значение владельца реестр по-прежнему отдаёт как написано."""
    import aurora_update as AU
    import sources_registry as R
    root = make_project(tmp)
    absolute = tmp / "abs_outside"
    rows = [("Up", "confluence-dc", "../outside"), ("Abs", "confluence-dc", str(absolute)),
            ("Old", "confluence-dc", "Sources/Wiki (old)"), ("Fine", "jira-dc", "Sources/FineNew")]
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + "\n\nsources:\n"
                   + "".join(f"  - id: {i}\n    module: {m}\n    path: {p}\n" for i, m, p in rows), encoding="utf-8")
    got = {i["id"]: i["path"] for i in R.instances(str(root))}
    assert got == {i: p for i, _m, p in rows}, got                               # чтение реестра не изменилось
    assert sorted(AU.mirror_dirs(root)) == ["Sources/FineNew", "Sources/Wiki (old)"], AU.mirror_dirs(root)
    assert sorted(AU.outside_mirror_dirs(root)) == sorted(["../outside", str(absolute)]), AU.outside_mirror_dirs(root)
    before = sorted(p.name for p in tmp.iterdir())
    for flags in (["--structure-only"], []):
        shutil.rmtree(root / "Sources" / "FineNew", ignore_errors=True)
        cp = run("aurora_update.py", str(root), *flags, "--apply", cwd=root)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert "Путь зеркала вне проекта — папка не создана: ../outside" in cp.stdout, cp.stdout
        assert f"папка не создана: {absolute}" in cp.stdout, cp.stdout
        assert sorted(p.name for p in tmp.iterdir()) == before, f"update создал папку вне проекта: {flags}"
        assert not (tmp / "outside").exists() and not absolute.exists()
        assert (root / "Sources" / "Wiki (old)").is_dir() and (root / "Sources" / "FineNew").is_dir()
    assert cfg.read_text(encoding="utf-8").count("path: ../outside\n") == 1, "update переписал значение владельца"
    assert {i["id"]: i["path"] for i in R.instances(str(root))} == {i: p for i, _m, p in rows}
