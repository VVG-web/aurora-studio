"""Проверки движка Aurora, часть 44. Каркас и помощники — tests/harness.py.

Метрика «файлов ядра на расширение» и храповик (BL-ARC-02, срез 1): `tests/ext_footprint.py`
считает, сколько файлов ядра знают расширения каждой точки (коннектор, раздел панели,
провайдер Git, провайдер LLM, вид артефакта, ассистент) по литералам из самих манифестов и
реестров; число не должно расти относительно `tests/ext_footprint_baseline.json` (снижение не красит,
печатается подсказка «обнови планку»).
Модель и сеть не используются; копии дерева — только во временных папках.

Conformance расширений (BL-ARC-02, срез 2): каждый манифест репозитория (коннектор, раздел
панели, скин, провайдер Git) проходит общий загрузчик без отчёта, id совпадает с папкой или
файлом и уникален в виде; фиктивное расширение каждого вида подключается без правки ядра, а
соседний битый манифест его не прячет и отвечает записью об ошибке.

Документ для авторов расширений (BL-ARC-02, срез 3): раздел «Расширения» в docs/architecture.md
и docs/en/architecture.md называет каждую точку метрики и ссылается на файл планки (чисел в таблице
нет: они хранятся только в планке).
"""
from __future__ import annotations

import ast
import contextlib
import importlib
import io
import json
import shutil
import sys
from pathlib import Path

from harness import KIT, test  # noqa: F401

_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

import ext_footprint as EF  # noqa: E402


def _kit_copy(tmp: Path) -> Path:
    """Копия ядра и реестров без чужих библиотек: на ней добавляют расширения и правят ядро."""
    dst = tmp / "kitcopy"
    ignore = shutil.ignore_patterns("vendor", "__pycache__", "*.pyc")
    for d in ("scripts", "cockpit", "reports", "connectors", "gitproviders"):
        shutil.copytree(KIT / d, dst / d, ignore=ignore)
    for f in EF.CORE_ROOT_FILES:
        if (KIT / f).is_file():
            shutil.copy2(KIT / f, dst / f)
    return dst


def _new_connector(root: Path, cid: str = "demox") -> None:
    d = root / "connectors" / cid
    d.mkdir(parents=True)
    (d / "connector.json").write_text(json.dumps({
        "id": cid, "title": "Демо", "kind": "board", "role": "tasks",
        "mirror": {"default_path": f"Sources/{cid.capitalize()}", "state": "update_log.md"},
        "run": {"script": f"{cid}_export.py", "command": f"sync:{cid}", "skill": f"{cid}-sync"},
        "auth": {"env_prefix": cid.upper()}, "requires": {"python": []}}, ensure_ascii=False),
        encoding="utf-8")


@test
def test_ext_footprint_within_baseline(_t):
    """Файлов ядра на расширение не больше планки: красный только рост; снижение — подсказка."""
    base = EF.load_baseline()
    assert base, "нет планки tests/ext_footprint_baseline.json — python3 tests/ext_footprint.py --write"
    fp = EF.footprint(KIT)
    problems, hints = EF.ratchet_verdict({n: len(f) for n, f in fp.items()}, base)
    assert not problems, (problems, "ядро начало знать расширения по имени: вынесите литерал в "
                          "манифест или реестр", {n: fp[n] for n in EF.ratcheted()})
    for h in hints:
        print("подсказка:", h)


@test
def test_ext_footprint_ratchet_fails_only_on_growth(_t):
    """Храповик: рост и точка без планки — проблема; равенство — тишина; снижение — подсказка, не падение."""
    names = EF.ratcheted()
    base = {n: 5 for n in names}
    # равенство
    assert EF.ratchet_verdict(dict(base), base) == ([], [])
    # снижение: не проблема, но подсказка про планку
    now = dict(base)
    now[names[0]] = 3
    problems, hints = EF.ratchet_verdict(now, base)
    assert problems == [], problems
    assert len(hints) == 1 and names[0] in hints[0] and "--write" in hints[0], hints
    # рост: проблема, подсказки нет
    now = dict(base)
    now[names[1]] = 6
    problems, hints = EF.ratchet_verdict(now, base)
    assert len(problems) == 1 and names[1] in problems[0] and hints == [], (problems, hints)
    # точка без планки: проблема
    problems, _h = EF.ratchet_verdict(dict(base), {n: 5 for n in names[1:]})
    assert len(problems) == 1 and names[0] in problems[0], problems
    # справочная точка (скин) планки не требует и на счёт не влияет
    assert "skin" not in names
    assert EF.ratchet_verdict({**base, "skin": 99}, base) == ([], [])


@test
def test_ext_footprint_assistant_counts_paths_not_bare_words(tmp: Path):
    """Ассистент считается по пути настройки, а не по голому id: слово `continue` в кавычках (как в
    git_sync.py или route_runner.py) счёт не меняет, путь настройки `.continue/...` — растит на один."""
    root = _kit_copy(tmp)
    before = EF.counts(root)
    (root / "scripts" / "zz_core_probe.py").write_text(
        'FIXES = ("abort", "continue", "vscode", "zed", "pi", "amp")\n'
        'def f(how):\n    return "continue"\n', encoding="utf-8")
    assert EF.counts(root) == before, (before, EF.counts(root))
    assert not any(f.endswith("zz_core_probe.py") for f in EF.footprint(root)["assistant"])
    (root / "scripts" / "zz_core_probe.py").write_text(
        'CFG = "~/.continue/mcpServers/aurora.yaml"\n', encoding="utf-8")
    after = EF.counts(root)
    assert after["assistant"] == before["assistant"] + 1, (before, after)
    assert "continue" in EF.footprint_detail(root)["assistant"]
    # id всё равно виден как расширение, даже если литералов у него нет
    assert "claude-code" in EF.footprint_detail(root)["assistant"]


@test
def test_ext_footprint_every_probe_finds_extensions(_t):
    """Каждая проба находит хотя бы одно расширение: метрика не обнуляется молча при переезде каталогов."""
    detail = EF.footprint_detail(KIT)
    assert set(detail) == set(EF.PROBES), detail.keys()
    for name, per in detail.items():
        assert per, f"проба {name} не нашла ни одного расширения — переехал каталог или реестр?"
    # конкретные опорные id: если исчезли, проба читает не то место
    assert {"jira-dc", "confluence-dc", "web"} <= set(detail["connector"]), detail["connector"].keys()
    assert "github" in detail["git_provider"], detail["git_provider"].keys()
    assert "openai" in detail["llm_provider"], detail["llm_provider"].keys()
    assert "us" in detail["artifact_kind"], detail["artifact_kind"].keys()
    assert "claude-code" in detail["assistant"], detail["assistant"].keys()
    assert "about" in detail["panel_module"], detail["panel_module"].keys()


@test
def test_ext_footprint_counts_core_edits_not_new_extensions(tmp: Path):
    """Новое расширение без правки ядра счёт не меняет; файл ядра с его литералом — плюс один."""
    root = _kit_copy(tmp)
    before = EF.counts(root)
    # 1. добавили коннектор и ничего не тронули в ядре: счёт прежний
    _new_connector(root)
    assert EF.counts(root) == before, (before, EF.counts(root))
    assert "demox" in EF.footprint_detail(root)["connector"]
    # 2. собственный скрипт запуска и папка расширения ядром не считаются
    (root / "scripts" / "demox_export.py").write_text('MIRROR = "Sources/Demox"\n', encoding="utf-8")
    (root / "connectors" / "demox" / "SKILL.md").write_text("Sources/Demox\n", encoding="utf-8")
    assert EF.counts(root) == before, (before, EF.counts(root))
    # 3. файл ядра с литералом зеркала нового коннектора: счёт коннекторов +1, остальные прежние
    (root / "scripts" / "zz_core_probe.py").write_text(
        '# ядро заучило путь зеркала\nPATH = "Sources/Demox"\n', encoding="utf-8")
    after = EF.counts(root)
    assert after["connector"] == before["connector"] + 1, (before, after)
    assert {k: v for k, v in after.items() if k != "connector"} == \
        {k: v for k, v in before.items() if k != "connector"}, (before, after)
    assert "scripts/zz_core_probe.py" in EF.footprint(root)["connector"]


@test
def test_ext_footprint_other_probes_react_to_literals(tmp: Path):
    """Литерал нового провайдера Git и нового раздела панели в ядре тоже растит счёт своей точки."""
    root = _kit_copy(tmp)
    before = EF.counts(root)
    pd = root / "gitproviders" / "forgex"
    pd.mkdir(parents=True)
    (pd / "provider.json").write_text(json.dumps({"id": "forgex", "title": "ForgeX"}), encoding="utf-8")
    md = root / "cockpit" / "modules" / "demomod"
    md.mkdir(parents=True)
    (md / "module.json").write_text(json.dumps({"id": "demomod"}), encoding="utf-8")
    assert EF.counts(root) == before
    (root / "scripts" / "zz_core_probe.py").write_text(
        'PROV = ("forgex",)\nURL = "/api/demomod"\n', encoding="utf-8")
    after = EF.counts(root)
    assert after["git_provider"] == before["git_provider"] + 1, (before, after)
    assert after["panel_module"] == before["panel_module"] + 1, (before, after)
    assert after["connector"] == before["connector"], (before, after)


@test
def test_ext_footprint_tolerates_bad_input(tmp: Path):
    """Битый манифест, не-UTF-8 файл ядра и пустое дерево не роняют метрику; пути — as_posix."""
    root = _kit_copy(tmp)
    before = EF.counts(root)
    bad = root / "connectors" / "broken"
    bad.mkdir(parents=True)
    (bad / "connector.json").write_text("{не json", encoding="utf-8")
    assert EF.counts(root) == before
    # чужая кодировка: байты 0xFF/0xFE и литерал рядом — файл читается с пропуском ошибок
    _new_connector(root)
    (root / "scripts" / "zz_core_probe.py").write_bytes(b"\xff\xfe# \x80 Sources/Demox\n")
    assert "scripts/zz_core_probe.py" in EF.footprint(root)["connector"]
    # пустое дерево: ноль везде, без исключений
    empty = tmp / "empty"
    empty.mkdir()
    assert set(EF.counts(empty).values()) == {0}
    # все пути — относительные с «/»
    for files in EF.footprint(root).values():
        for rel in files:
            assert "\\" not in rel and not Path(rel).is_absolute(), rel


@test
def test_ext_footprint_module_runs_on_python_311(_t):
    """Модуль метрики — стандартная библиотека и синтаксис 3.11 (рабочий минимум движка)."""
    src = (Path(EF.__file__)).read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 11))
    base = json.loads(EF.BASELINE.read_text(encoding="utf-8"))
    assert set(base["counts"]) == set(EF.ratcheted()), base


# ----------------------------------------------- срез 2: conformance расширений


def _ck():
    """Панель как модуль (без запуска сервера): у неё `modules()` и `skins()`."""
    sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


def _put(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                    encoding="utf-8")
    return path


# Схема provider.json: проверяет её только этот тест, в боевом коде провайдеров Git читает
# `git_sync` сам; общий загрузчик (`aurora_common.load_manifest`) видов не знает и берёт схему
# из `EXT_SCHEMAS`, куда на время вызова её кладёт `_load_provider`.
_PROVIDER_SCHEMA = {
    "required": {"id": str, "title": str},
    "optional": {"version": str, "since": str, "api": str, "auth": list,
                 "about": "i18n", "requires": dict, "api_version": int},
    "nested": {},
}


def _load_provider(path, lenient: bool = False):
    """`load_manifest(path, "provider")` с локальной схемой провайдера: после вызова схемы в загрузчике нет."""
    import aurora_common as C
    assert "provider" not in C.EXT_SCHEMAS, "схема провайдера вернулась в боевой загрузчик"
    C.EXT_SCHEMAS["provider"] = _PROVIDER_SCHEMA
    try:
        return C.load_manifest(path, "provider", lenient=lenient)
    finally:
        del C.EXT_SCHEMAS["provider"]


def _repo_manifests() -> dict:
    """{вид: [(id по папке или файлу, путь манифеста)]} — всё, что лежит в репозитории."""
    return {
        "connector": [(p.parent.name, p) for p in sorted((KIT / "connectors").glob("*/connector.json"))],
        "module": [(p.parent.name, p) for p in sorted((KIT / "cockpit" / "modules").glob("*/module.json"))],
        "provider": [(p.parent.name, p) for p in sorted((KIT / "gitproviders").glob("*/provider.json"))],
    }


@test
def test_every_repo_manifest_passes_the_envelope(_t):
    """Каждый connector.json, module.json и provider.json репозитория читается загрузчиком без
    отчёта (строго), его id совпадает с папкой и уникален в виде; расширений каждого вида не меньше базы."""
    import aurora_common as C
    found = _repo_manifests()
    for kind, least in (("connector", 3), ("module", 17), ("provider", 4)):
        items = found[kind]
        assert len(items) >= least, (kind, [n for n, _ in items])
        ids = []
        for name, path in items:
            got, rep = _load_provider(path) if kind == "provider" else C.load_manifest(path, kind)
            assert got is not None and rep == [], (kind, name, rep)
            assert got["id"] == name, (kind, name, got["id"])
            assert got["api_version"] == 1, (kind, name, got["api_version"])
            ids.append(got["id"])
        assert len(ids) == len(set(ids)), (kind, ids)
    # провайдеры: схема требует список способов входа и название на каждом языке описания
    for name, path in found["provider"]:
        got, _ = _load_provider(path)
        assert got["auth"] and set(got["about"]) >= {"ru", "en"}, (name, got)


@test
def test_every_repo_skin_passes_the_envelope(_t):
    """Скины репозитория проходят `skins()` без ошибок, их id совпадают с файлами и не повторяются."""
    ck = _ck()
    files = sorted(p.stem for p in (KIT / "cockpit" / "skins").glob("*.css"))
    got = ck.skins()
    assert len(files) >= 5 and [r["id"] for r in got] == files, ([r["id"] for r in got], files)
    assert not [r for r in got if "error" in r], got
    assert len({r["id"] for r in got}) == len(got)


@test
def test_provider_loader_reports_instead_of_raising(tmp: Path):
    """Битый provider.json — отчёт, а не исключение; сосед читается; имя в отчёте — из папки."""
    root = tmp / "gitproviders"
    good = {"id": "demo", "title": "Demo", "auth": ["token"], "api": "/api", "about": {"ru": "Р", "en": "E"}}
    _put(root / "demo" / "provider.json", good)
    cases = {
        "junk": "{ не json",
        "noid": {"title": "Нет id"},
        "notitle": {"id": "notitle"},
        "future": {**good, "id": "future", "api_version": 99},
        "authstr": {**good, "id": "authstr", "auth": "token"},
        "titlenum": {**good, "id": "titlenum", "title": 5},
        "aboutnum": {**good, "id": "aboutnum", "about": {"ru": 5}},
        "list": "[1]",
        "empty": "",
    }
    for name, data in cases.items():
        _put(root / name / "provider.json", data)
    (root / "nofile").mkdir()
    res = {}
    for d in sorted(root.iterdir()):
        res[d.name] = _load_provider(d / "provider.json")      # не бросает
    assert res["demo"][0]["id"] == "demo" and res["demo"][1] == []
    for name in cases:
        got, rep = res[name]
        assert got is None and len(rep) == 1 and "provider «%s»" % name in rep[0], (name, got, rep)
    assert res["nofile"][0] is None and "provider «nofile»" in res["nofile"][1][0], res["nofile"]
    assert "v99" in res["future"][1][0] and "auth" in res["authstr"][1][0]
    # мягкий режим тоже не роняет: необязательное поле не того типа — отброшено, обязательное — отказ
    got, notes = _load_provider(root / "authstr" / "provider.json", lenient=True)
    assert got and "auth" not in got and notes, (got, notes)
    assert _load_provider(root / "titlenum" / "provider.json", lenient=True)[0] is None


@test
def test_fictive_panel_section_is_found_next_to_broken_manifests(tmp: Path):
    """Новый раздел панели — только папка с module.json: он в `modules()` без правки ядра, а соседи
    (не JSON, нет id, api из будущего, чужой id) не прячут его и дают записи об ошибке."""
    root = tmp / "modules"
    _put(root / "fictive" / "module.json", {"id": "fictive", "name": {"ru": "Фиктивный", "en": "Fictive"},
                                            "group": "project", "order": 7})
    _put(root / "junk" / "module.json", "{ не json")
    _put(root / "noid" / "module.json", {"name": {"ru": "Без id"}})
    _put(root / "future" / "module.json", {"id": "future", "name": {"ru": "Б"}, "api_version": 99})
    _put(root / "other" / "module.json", {"id": "elsewhere", "name": {"ru": "Чужой"}})
    ck = _ck()
    keep = ck.MODULES_DIR
    ck.MODULES_DIR = str(root)
    try:
        by = {m["id"]: m for m in ck.modules()}
    finally:
        ck.MODULES_DIR = keep
    assert set(by) == {"fictive", "junk", "noid", "future", "other"}, sorted(by)
    ok = by["fictive"]
    assert "error" not in ok and "warn" not in ok and ok["order"] == 7 and ok["name"]["en"] == "Fictive", ok
    for bad in ("junk", "noid", "future", "other"):
        assert set(by[bad]) == {"id", "error"} and by[bad]["error"], (bad, by[bad])
    assert "v99" in by["future"]["error"]


@test
def test_fictive_skin_is_found_next_to_broken_skins(tmp: Path):
    """Новый скин — только файл .css с шапкой: он в `skins()` без правки ядра; не UTF-8 и api из
    будущего его не прячут и отвечают записью об ошибке."""
    root = tmp / "skins"
    root.mkdir()
    (root / "fictive.css").write_text("/* name: Фиктивный\n   api: 1\n   about: Проба. */\n", encoding="utf-8")
    (root / "latin.css").write_bytes(b"/* name: \xff\xfe */\n")
    (root / "future.css").write_text("/* name: Б\n   api: 99 */\n", encoding="utf-8")
    ck = _ck()
    keep = ck.SKINS_DIR
    ck.SKINS_DIR = str(root)
    try:
        by = {r["id"]: r for r in ck.skins()}
    finally:
        ck.SKINS_DIR = keep
    assert set(by) == {"fictive", "latin", "future"}, sorted(by)
    assert "error" not in by["fictive"] and by["fictive"]["name"] == "Фиктивный" and by["fictive"]["api"] == 1
    for bad in ("latin", "future"):
        assert set(by[bad]) == {"id", "error"} and by[bad]["error"], (bad, by[bad])


@test
def test_fictive_connector_is_found_next_to_broken_manifests(tmp: Path):
    """Новый коннектор — только connector.json: он в реестре источников без правки ядра; соседи
    (не JSON, нет id, api из будущего) не прячут его, и каждый назван в отчёте реестра."""
    import sources_registry as R
    R._REPORT.clear()
    root = tmp / "proj"
    conn = root / ".aurora" / "connectors"
    _put(conn / "fictive.json", {
        "id": "fictive", "title": "Фиктивный", "kind": "board", "role": "tasks",
        "mirror": {"default_path": "Sources/Fictive", "state": "update_log.md"},
        "run": {"script": "fictive_export.py", "command": "sync:fictive", "skill": "fictive-sync"}})
    _put(conn / "junk.json", "{ не json")
    _put(conn / "noid.json", {"kind": "board", "mirror": {}, "run": {}})
    _put(conn / "future.json", {
        "id": "future", "kind": "board", "mirror": {}, "run": {}, "api_version": 99})
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            mods = R.installed(str(root))
            rep = "\n".join(R.registry_report())
    finally:
        R._REPORT.clear()
    assert set(mods) == {"fictive"} and mods["fictive"]["mirror"]["default_path"] == "Sources/Fictive", sorted(mods)
    for name in ("junk", "noid", "future"):
        assert "connector «%s»" % name in rep, (name, rep)


def _ext_doc_rows(rel: str) -> dict[str, str]:
    """{ключ точки: остаток строки} из таблицы раздела «Расширения» документа: ключ — в обратных
    кавычках в первой ячейке, например «Коннектор (`connector`)»."""
    import re
    text = (KIT / rel).read_text(encoding="utf-8")
    head = re.search(r"^## 10\. .*$", text, re.M)
    assert head, (rel, "нет раздела «10. Расширения»")
    rows: dict[str, str] = {}
    for line in text[head.end():].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.search(r"`(\w+)`", cells[0]) if line.startswith("|") and len(cells) >= 2 else None
        if m:
            rows[m.group(1)] = " | ".join(cells[1:])
    return rows


@test
def test_extensions_doc_names_every_probe_and_points_to_baseline_file(_t):
    """Раздел «Расширения» в ru и en называет каждую точку метрики и ссылается на файл планки, который
    существует; чисел планки в таблице нет (они хранятся только в `ext_footprint_baseline.json`)."""
    import re
    assert (KIT / "tests" / "ext_footprint_baseline.json").is_file()
    assert (KIT / "tests" / "ext_footprint.py").is_file()
    for rel in ("docs/architecture.md", "docs/en/architecture.md"):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "tests/ext_footprint_baseline.json" in text, (rel, "нет ссылки на файл планки")
        rows = _ext_doc_rows(rel)
        for name in EF.PROBES:
            assert name in rows, (rel, name, "точка метрики не названа в таблице")
            assert not re.match(r"\d+\b", rows[name]), (rel, name, "число в таблице: оно живёт в планке")


# ------------------------------------------------------------------ жизнь раздела панели (BL-ARC-11, срез 1)

LIFECYCLE_HARNESS = r"""
const faults = [], toasts = [];
const t = (key, vars) => key + ":" + JSON.stringify(vars || {});
const showFault = (m) => { faults.push(m); };
const toast = (text, kind) => { toasts.push({text, kind}); };
const NOOP = () => {};
const el = (tag, attrs) => ({tag, attrs, classList: {toggle: NOOP}, remove: NOOP});
const $ = () => ({append: NOOP});
const $$ = () => [];
const S = {view: "none", lang: "ru", project: null, health: null, state: {}};
const location = {hash: ""};
const TOKEN = "t", location_ = location;
const api = NOOP, openLink = NOOP, runStep = NOOP, runWatched = NOOP, busyElsewhere = NOOP, openRun = NOOP, openDoc = NOOP,
  reloadHealth = NOOP, runRoute = NOOP, poll = NOOP, restartPanel = NOOP, ago = NOOP, kb = NOOP, histWhen = NOOP, esc = NOOP,
  tick = NOOP, md = NOOP, mdLite = NOOP, rtime = NOOP, howLong = NOOP, metricCard = NOOP, metric = NOOP, goRoute = NOOP,
  goCmd = NOOP, kindChip = NOOP, skillLine = NOOP, copyButton = NOOP, engineWord = NOOP, markdownEditor = NOOP, hasPart = NOOP,
  waitMark = NOOP, healthAt = NOOP, stampText = NOOP, kbInfo = NOOP, openIssue = NOOP, issueUrl = NOOP, openPath = NOOP,
  isEngineCmd = NOOP, hideDev = NOOP, openProject = NOOP, lastRun = NOOP, rcMark = NOOP, auraWhy = NOOP;
const applyI18n = NOOP, confirmLeave = () => true, devOnlyView = () => false, devSectionsOn = () => false;
const renderOverview = NOOP, renderProjBadge = NOOP, drawBridgeStamp = NOOP, navBadges = NOOP;
const requestAnimationFrame = (f) => { setTimeout(f, 0); return 1; };
let IMPORT = async () => { throw new Error("нет модуля"); };
const globalThis_fetch = async () => ({ok: true, text: async () => "<div></div>"});
const fetch = globalThis_fetch;
@MODULES@
@CTX@
@HEALTH@
@SHOW@
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
const log = [];
const tickAll = () => new Promise(r => setTimeout(r, 5));
function add(id, mod, ns){
  MODULES.set(id, {manifest: {id}, mod, ns: ns || null, section: {classList: {toggle: NOOP}}, subs: null, stale: false, shown: false});
  return MODULES.get(id);
}
const mk = (id, extra) => Object.assign({
  mount(){}, refresh(){ log.push(id + ".refresh"); }}, extra || {});

(async () => {
  // 1. события: старый раздел, ленивые на виду и скрытые, подписчик
  const legacy = add("L", mk("L"));
  const hidden = add("H", mk("H"), {lifecycle: 1});
  const visible = add("V", mk("V"), {lifecycle: 1});
  const named = add("R", mk("R"), {refreshOn: ["health"]});
  const sub = add("SUB", mk("SUB", {lifecycle: 1}));
  const ctxSub = moduleCtx("SUB", {});
  ctxSub.on("data", () => log.push("SUB.fn"));
  S.view = "V";
  await refreshModules();
  const count = (s) => log.filter(x => x === s).length;
  check(count("L.refresh") === 1, "раздел без экспортов: refresh на «data» как прежде, а их " + count("L.refresh"));
  check(count("H.refresh") === 0 && hidden.stale === true, "скрытый lifecycle-раздел: 0 refresh и stale=true");
  check(count("V.refresh") === 1 && visible.stale === false, "видимый lifecycle-раздел: ровно один refresh");
  check(count("R.refresh") === 1, "старый раздел с refreshOn на «data» тоже обновляется");
  check(count("SUB.fn") === 1 && count("SUB.refresh") === 0 && sub.stale === false,
        "подписчик скрытого раздела получает fn, а не refresh");
  log.length = 0;
  await dispatchModules("health");
  check(log.join() === "R.refresh", "«health» зовёт только refreshOn-разделы, а позвал: " + log.join());
  // ленивый раздел с refreshOn на «health»: на виду — refresh, скрытый — stale
  add("HW", mk("HW"), {lifecycle: 1, refreshOn: ["health"]});
  add("VW", mk("VW"), {lifecycle: 1, refreshOn: ["health"]}); 
  S.view = "VW";
  log.length = 0;
  await dispatchModules("health");
  check(log.includes("VW.refresh") && !log.includes("HW.refresh") && MODULES.get("HW").stale, "refreshOn у lifecycle-разделов: на виду refresh, скрытый — stale: " + log.join());
  // healthChanged: часть чужого проекта никого не будит, своего — зовёт «health»
  S.project = {name: "mine"}; S.health = null;
  log.length = 0;
  healthChanged({name: "other"}); await tickAll();
  check(log.length === 0, "часть здоровья чужого проекта не будит разделы");
  healthChanged(S.project); await tickAll();
  check(log.includes("R.refresh") && !log.includes("L.refresh"), "часть своего проекта будит refreshOn-разделы, не остальные: " + log.join());
  // падение refresh одного раздела не мешает другому
  const bad = add("BAD", {mount(){}, refresh(){ throw new Error("бум"); }});
  const after = add("AFTER", mk("AFTER"));
  log.length = 0; faults.length = 0;
  await refreshModules();
  check(log.includes("AFTER.refresh") && faults.length === 1 && faults[0].includes("бум"), "сбой refresh не мешает следующему разделу");
  MODULES.delete("BAD"); MODULES.delete("AFTER");

  // 2. порядок хуков при переходе A -> B
  for (const id of ["L", "H", "V", "R", "SUB", "HW", "VW"]) MODULES.delete(id);
  const hooks = (id) => ({onShow(){ log.push(id + ".onShow"); }, onHide(){ log.push(id + ".onHide"); }});
  const A = add("A", mk("A", hooks("A")));
  const B = add("B", mk("B", hooks("B")), {lifecycle: 1});
  S.view = "A"; A.shown = true;
  log.length = 0;
  show("B"); await tickAll();
  check(log.join() === "A.onHide,B.refresh,B.onShow", "A->B: onHide, refresh, onShow, а было: " + log.join());
  log.length = 0;
  show("B"); await tickAll();
  check(log.join() === "B.refresh", "повторный show того же раздела onShow не дублирует: " + log.join());
  log.length = 0;
  show("A"); await tickAll();
  check(log.join() === "B.onHide,A.refresh,A.onShow", "B->A: " + log.join());
  // ушли, пока раздел отрисовывался: onShow не зовётся
  let release;
  const slow = add("SLOW", {mount(){}, refresh(){ return new Promise(r => { release = r; }); }, onShow(){ log.push("SLOW.onShow"); }, onHide(){ log.push("SLOW.onHide"); }});
  log.length = 0;
  show("SLOW"); await tickAll();
  show("A"); await tickAll();
  release(); await tickAll();
  check(!log.includes("SLOW.onShow") && !log.includes("SLOW.onHide"), "раздел, из которого ушли до конца отрисовки, не получает onShow: " + log.join());
  MODULES.delete("SLOW");

  // 3. падение хука одного раздела не мешает другому и переходу
  const F = add("F", mk("F", {onHide(){ throw new Error("хук упал"); }, onShow(){}}));
  const G = add("G", mk("G", hooks("G")));
  S.view = "none"; show("F"); await tickAll();
  faults.length = 0; log.length = 0;
  show("G"); await tickAll();
  check(S.view === "G" && log.includes("G.onShow"), "переход состоялся, хук следующего раздела вызван: " + log.join());
  check(faults.length === 1 && faults[0].includes("onHide") && faults[0].includes("хук упал"), "сбой хука показан одним сообщением: " + faults.join("|"));

  // 4. раздел не поднялся: dispose и чистое состояние
  const rec = {manifest: {id: "D"}, mod: null, ns: null, section: null, subs: null, stale: false, shown: false};
  MODULES.set("D", rec);
  let disposed = 0;
  IMPORT = async () => ({default: {mount(ctx){ ctx.on("data", NOOP); throw new Error("mount упал"); }, dispose(){ disposed++; }}});
  toasts.length = 0;
  await mountModule("D");
  check(disposed === 1 && rec.mod === null && rec.subs === null && toasts.length === 1, "сбой mount: dispose один раз, раздел снят, подписки сброшены");

  // 5. версия и описание ctx
  const c = moduleCtx("A", {});
  check(c.api_version === @API@, "ctx.api_version = EXT_API_VERSION, а " + c.api_version);
  check(c.lifecycle === 1, "ctx.lifecycle = 1");
  const off = c.on("data", NOOP);
  check(typeof off === "function" && MODULES.get("A").subs.get("data").size === 1, "ctx.on запоминает подписку");
  off();
  check(MODULES.get("A").subs.get("data").size === 0, "отписка снимает подписку");
  console.log(JSON.stringify({fails}));
})().catch(e => { console.log(JSON.stringify({fails: ["исключение: " + (e && e.stack || e)]})); });
"""


@test
def test_module_lifecycle_under_node(tmp: Path):
    """Жизненный цикл раздела панели под подставным DOM (нужен node, иначе проверка пропускается): после «data»
    скрытый раздел с `lifecycle` не зовёт refresh и помечен устаревшим, видимый обновляется, раздел без экспортов
    обновляется как раньше; «health» будит только разделы с `refreshOn`; подписчик скрытого раздела получает свою
    функцию; onHide/refresh/onShow идут в порядке перехода, раздел, из которого ушли до конца отрисовки, onShow не
    получает; сбой хука или refresh не мешает соседям; сбой mount зовёт dispose; `ctx.api_version` равен
    `EXT_API_VERSION`."""
    import aurora_common as C
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    assert f"const CTX_API = {C.EXT_API_VERSION};" in panel, "CTX_API разошёлся с aurora_common.EXT_API_VERSION"

    def cut(start: str, end: str, keep_end: bool = False) -> str:
        a = panel.index(start)
        b = panel.index(end, a)
        return panel[a:b + (len(end) if keep_end else 0)]
    modules = cut("const MODULES = new Map();", "async function loadModules")
    ctx = cut("function moduleCtx(id, root){", "// Подписи разделов в меню")
    health = cut("let HEALTH_FRAME = 0", "async function prefetchHealth")
    show = cut("function show(view, payload){", "\n}\n", keep_end=True)
    # статическая проверка: имён разделов в healthChanged больше нет
    for name in ("mirrors", "install", '["health"'):
        assert name not in health, f"в healthChanged осталось имя «{name}»"
    assert 'dispatchModules("health")' in health and "for (const id of" not in health
    # сам импорт раздела заменён на подставной
    assert 'import(url("view.js"))' in ctx
    ctx = ctx.replace('import(url("view.js"))', 'IMPORT(url("view.js"))')
    node = shutil.which("node")
    if not node:
        return
    code = (LIFECYCLE_HARNESS.replace("@MODULES@", modules).replace("@CTX@", ctx).replace("@HEALTH@", health)
            .replace("@SHOW@", show).replace("@API@", str(C.EXT_API_VERSION)))
    src = tmp / "lifecycle_panel.mjs"
    src.write_text(code, encoding="utf-8")
    import subprocess
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert result["fails"] == [], "\n".join(result["fails"])


@test
def test_first_modules_declare_refresh_events(_t):
    """«Здоровье», «Зеркала» и «Установка» сами называют событие, на которое перерисовываются (`refreshOn` с «health»),
    а «Установка» — ещё и ленивое обновление (`lifecycle = 1`): ядро их имён больше не знает."""
    for mid in ("health", "mirrors", "install"):
        text = (KIT / "cockpit" / "modules" / mid / "view.js").read_text(encoding="utf-8")
        assert 'export const refreshOn = ["health"];' in text, mid
        assert ("export const lifecycle = 1;" in text) == (mid == "install"), mid


@test
def test_module_lifecycle_is_documented(_t):
    """Справочник автора раздела (ru) и описание панели (en) называют хуки, ленивое обновление и события."""
    ru = (KIT / "cockpit" / "modules" / "README.md").read_text(encoding="utf-8")
    en = (KIT / "docs" / "en" / "control-panel.md").read_text(encoding="utf-8")
    for word in ("lifecycle", "onShow", "onHide", "dispose", "refreshOn", "ctx.on", "api_version"):
        assert word in ru, f"README раздела: нет «{word}»"
        assert word in en, f"docs/en/control-panel.md: нет «{word}»"
    assert "как прежде" in ru and "works as before" in en
