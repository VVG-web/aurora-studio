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
            "for": ver, "behind": ck.minor(ver) != ck.minor(ck.kit_version()),
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
    # английский: ни одного русского слова в причинах
    import re as _re
    assert len(en) == 6 and not [e for e in en if _re.search("[а-яА-ЯёЁ]", e["error"])], en
    assert any("extension API v2" in e["error"] for e in en), en


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
