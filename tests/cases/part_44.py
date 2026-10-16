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
import re
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
const applyI18n = NOOP, confirmLeave = () => true, devOnlyView = () => false, devSectionsOn = () => false, profileHidden = () => false;
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

  // 2б. медленный onShow: уход из раздела не обгоняет его, onHide приходит после конца onShow
  let relShow;
  const slowShow = add("SS", {mount(){}, refresh(){}, onShow(){ log.push("SS.onShow.start"); return new Promise(r => { relShow = () => { log.push("SS.onShow.end"); r(); }; }); },
    onHide(){ log.push("SS.onHide"); }});
  S.view = "A"; log.length = 0;
  show("SS"); await tickAll();
  show("A"); await tickAll();
  check(log.includes("SS.onShow.start") && !log.includes("SS.onHide"), "onHide ждёт конца медленного onShow: " + log.join());
  relShow(); await tickAll();
  check(log.join().indexOf("SS.onShow.end") < log.join().indexOf("SS.onHide") && log.includes("SS.onHide"), "после onShow приходит onHide: " + log.join());
  MODULES.delete("SS");
  // 2в. refresh упал при первом показе: сообщение есть, onShow всё равно приходит
  add("RF", {mount(){}, refresh(){ throw new Error("первый refresh упал"); }, onShow(){ log.push("RF.onShow"); }});
  S.view = "A"; log.length = 0; faults.length = 0;
  show("RF"); await tickAll();
  check(log.includes("RF.onShow") && MODULES.get("RF").shown === true, "сбой первого refresh не лишает раздел onShow: " + log.join());
  check(faults.length === 1 && faults[0].includes("первый refresh упал"), "сбой первого refresh показан: " + faults.join("|"));
  MODULES.delete("RF");
  // 2г. подписка ctx.on у раздела без lifecycle не зовётся; сбой подписчика не мешает остальным и соседям
  add("NL", mk("NL")); moduleCtx("NL", {}).on("health", () => log.push("NL.fn"));
  const lz = add("LZ", mk("LZ"), {lifecycle: 1});
  const cz = moduleCtx("LZ", {});
  cz.on("health", () => { throw new Error("подписчик 1"); });
  cz.on("health", () => log.push("LZ.fn2"));
  S.view = "A"; log.length = 0; faults.length = 0;
  await dispatchModules("health");
  check(!log.includes("NL.fn") && !log.includes("NL.refresh"), "ctx.on без lifecycle игнорируется: " + log.join());
  check(log.includes("LZ.fn2") && faults.length === 1 && faults[0].includes("подписчик 1"), "сбой подписчика не прерывает остальных: " + log.join() + "|" + faults.join("|"));
  MODULES.delete("NL"); MODULES.delete("LZ");
  // 2д. «health» обслуживает разделы одновременно: медленный не задерживает соседа
  let relSlow;
  add("W1", {mount(){}, refresh(){ return new Promise(r => { relSlow = r; }); }, refreshOn: ["health"]});
  add("W2", mk("W2", {refreshOn: ["health"]}));
  log.length = 0;
  const pending = dispatchModules("health"); await tickAll();
  check(log.includes("W2.refresh"), "медленный раздел не задерживает соседа на «health»: " + log.join());
  relSlow(); await pending;
  MODULES.delete("W1"); MODULES.delete("W2");

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
    show = cut("function show(view, payload, opts){", "\n}\n", keep_end=True)
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
    и объявили ленивое обновление (`lifecycle = 1`): ядро их имён больше не знает."""
    for mid in ("health", "mirrors", "install"):
        text = (KIT / "cockpit" / "modules" / mid / "view.js").read_text(encoding="utf-8")
        assert 'export const refreshOn = ["health"];' in text, mid
        assert "export const lifecycle = 1;" in text, mid


@test
def test_module_lifecycle_is_documented(_t):
    """Справочник автора раздела (ru) и описание панели (en) называют хуки, ленивое обновление и события."""
    ru = (KIT / "cockpit" / "modules" / "README.md").read_text(encoding="utf-8")
    en = (KIT / "docs" / "en" / "control-panel.md").read_text(encoding="utf-8")
    ru_docs = (KIT / "docs" / "control-panel-ui-requirements.md").read_text(encoding="utf-8")
    for word in ("lifecycle", "onShow", "onHide", "dispose", "refreshOn", "ctx.on", "api_version"):
        assert word in ru, f"README раздела: нет «{word}»"
        assert word in en, f"docs/en/control-panel.md: нет «{word}»"
        assert word in ru_docs, f"docs/control-panel-ui-requirements.md: нет «{word}»"
    assert "как прежде" in ru and "works as before" in en and "как прежде" in ru_docs
    # ctx.on работает только у раздела с lifecycle — об этом сказано везде
    assert "только у\nраздела с `lifecycle = 1`" in ru or "только у раздела с `lifecycle = 1`" in ru
    assert "only for a section with `lifecycle = 1`" in en


# ------------------------------------------------------------------ шумные разделы на ленивом обновлении (BL-ARC-11, срез 2)

NOISY_HARNESS = r"""
import {pathToFileURL} from "node:url";
const rst = setImmediate;
const tick = async () => { for (let i = 0; i < 10; i++) await new Promise(r => rst(r)); };
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
// Подставные таймеры: ход времени — вызовом fire(), настоящих ожиданий нет.
const timers = [];
globalThis.setTimeout = (fn, ms) => { const h = {fn, ms, off: false, done: false}; timers.push(h); return h; };
globalThis.clearTimeout = (h) => { if (h) h.off = true; };
globalThis.requestAnimationFrame = () => 0;
globalThis.confirm = () => true;
globalThis.localStorage = {getItem: () => null, setItem: () => {}};
const pending = (ms) => timers.filter(h => !h.off && !h.done && (ms === undefined || h.ms === ms));
async function fire(ms){ const run = pending(ms); for (const h of run){ h.done = true; h.fn(); } await tick(); return run.length; }
// Подставной DOM: любое обращение к узлу возвращает узел, присвоенное запоминается.
function node(){
  const store = {};
  const p = new Proxy(function(){}, {
    get(_, k){ if (typeof k === "symbol" || k === "then") return undefined; return k in store ? store[k] : p; },
    set(_, k, v){ store[k] = v; return true; },
    apply(){ return p; },
  });
  return p;
}
const ELS = [];
const el = (tag, attrs, ...kids) => { ELS.push({tag, attrs: attrs || {}, kids}); return node(); };
const faults = [];
const showFault = (m) => { faults.push(m); };
const t = (k) => k;
const S = {view: "none", lang: "ru"};
const CTXS = {};
function moduleCtx(id){ return CTXS[id]; }
@MODULES@
@DISPATCH@
function mkctx(id, impl, extra){
  const c = {id, root: node(), calls: [], toasts: [], t: (k) => k, el, $: () => node(), $$: () => [],
    fmt: node(), ui: node(), project: {path: "/p"}, lang: "ru", state: {kit: {}}, show: () => {},
    toast: (m, k) => c.toasts.push(m), ...(extra || {}),
    api: async (path, opts) => { c.calls.push(path); return impl(path, opts); }};
  Object.defineProperty(c, "view", {get: () => S.view});
  CTXS[id] = c;
  return c;
}
async function load(id){
  const ns = await import(pathToFileURL(@DIR@ + "/" + id + ".mjs").href);
  return ns;
}
function register(id, ns){
  MODULES.set(id, {manifest: {id}, mod: ns.default || ns, ns, section: node(), subs: null, stale: false, shown: false, hooks: null});
  return MODULES.get(id);
}
const apiCount = (c, part) => c.calls.filter(p => p.includes(part)).length;

(async () => {
  const ns = {};
  for (const id of ["bots", "cron", "models", "about"]){
    ns[id] = await load(id);
    check(ns[id].lifecycle === 1, id + ": нет `export const lifecycle = 1`");
  }

  // ---- about: скрытый раздел на «data» не ходит на /api/about, видимый — ходит
  {
    const c = mkctx("about", () => ({kit: "1", ui: "1", commands: 1, commit: "", license: "MIT", repo: "",
      branch: "", author: "", path: "", releases: []}));
    ns.about.mount(c); register("about", ns.about);
    S.view = "about"; await dispatchModules("data");
    check(apiCount(c, "/api/about") === 1, "about на виду: один /api/about на «data», а " + apiCount(c, "/api/about"));
    S.view = "other"; await dispatchModules("data"); await dispatchModules("data");
    check(apiCount(c, "/api/about") === 1, "about скрыт: «data» не зовёт /api/about, а их " + apiCount(c, "/api/about"));
    check(MODULES.get("about").stale === true, "about скрыт: помечен устаревшим");
    check(faults.length === 0, "about: сбой refresh " + faults.join("|"));
    MODULES.delete("about");
  }

  // ---- models: скрытый раздел не стирает несохранённую правку и не зовёт /api/models
  {
    const cfg = {providers: [], capabilities: {llm: {roles: []}, ocr: {roles: []}, embeddings: {roles: []}}};
    const c = mkctx("models", (p) => p.startsWith("/api/models") ? {models: cfg, projects: [], path: "x"} : {});
    ns.models.mount(c); register("models", ns.models);
    S.view = "models"; ELS.length = 0;
    await dispatchModules("data");
    check(apiCount(c, "/api/models") === 1, "models на виду: один /api/models, а " + apiCount(c, "/api/models"));
    const add = ELS.filter(e => e.kids.includes("models.add_provider")).pop();
    check(!!add, "models: нет кнопки добавления провайдера в подставном DOM");
    ELS.length = 0;
    add.attrs.onclick();                                        // правка формы: DIRTY = true
    const pingBtn = () => ELS.filter(e => e.attrs["data-llm"] === "ping").pop();
    c.toasts.length = 0; pingBtn().attrs.onclick(); await tick();
    check(c.toasts.includes("models.ping_save_first"), "models: правка не сохранена (DIRTY не выставлен): " + c.toasts.join());
    c.calls.length = 0; c.toasts.length = 0;
    S.view = "other"; await dispatchModules("data"); await dispatchModules("data");
    check(c.calls.length === 0, "models скрыт: «data» не ходит на сервер, а было " + c.calls.join());
    // правка жива: refresh не звался, DIRTY не сброшен — «Проверить» по-прежнему просит сначала сохранить
    pingBtn().attrs.onclick(); await tick();
    check(c.toasts.includes("models.ping_save_first") && c.calls.length === 0,
          "models скрыт: несохранённая правка не должна пропасть от «data»: " + c.toasts.join() + "|" + c.calls.join());
    check(faults.length === 0, "models: сбой refresh " + faults.join("|"));
    MODULES.delete("models");
  }

  // ---- cron: таймер слежения гасится в onHide, поднимается в onShow
  {
    const cur = {id: "r1", items: []};
    const c = mkctx("cron", () => ({current: cur, tasks: [], runs: [], routes: [], projects: [], queue: []}));
    ns.cron.mount(c); register("cron", ns.cron);
    S.view = "cron";
    await dispatchModules("data");
    check(apiCount(c, "/api/cron") === 1 && pending(4000).length === 1, "cron на виду: один запрос и один таймер слежения");
    await ns.cron.onShow(c);
    check(pending(4000).length === 1, "cron onShow не плодит таймеры: " + pending(4000).length);
    await fire(4000);
    check(apiCount(c, "/api/cron") === 2 && pending(4000).length === 1, "cron: таймер сработал и взвёлся снова");
    await ns.cron.onHide(c);
    check(pending().length === 0, "cron onHide: таймер погашен");
    const before = c.calls.length;
    for (let i = 0; i < 3; i++) await fire();                  // «прошло больше LIVE_MS» трижды
    check(c.calls.length === before && pending().length === 0, "cron скрыт: ни одного нового запроса и таймера");
    // запрос в полёте при уходе: ответ не взводит новый таймер
    let release; let gate = false;
    c.api = async (path) => { c.calls.push(path); if (gate) await new Promise(r => { release = r; }); return {current: cur, tasks: [], runs: [], routes: [], projects: [], queue: []}; };
    await ns.cron.onShow(c); gate = true;
    await fire(4000);                                          // таймер сработал, запрос ждёт
    await ns.cron.onHide(c);
    release(); await tick();
    check(pending().length === 0, "cron: ответ, пришедший после onHide, не взводит таймер: " + pending().length);
    gate = false;
    await ns.cron.onShow(c);
    check(pending(4000).length === 1, "cron onShow: таймер поднят снова");
    const n = c.calls.length;
    await fire(4000);
    check(c.calls.length === n + 1, "cron после onShow: слежение идёт");
    await ns.cron.dispose(c);
    check(pending().length === 0, "cron dispose: таймер снят");
    // скрытый раздел не ходит за данными на «data»
    S.view = "other"; c.calls.length = 0;
    await dispatchModules("data");
    check(c.calls.length === 0, "cron скрыт: «data» не зовёт /api/cron");
    check(faults.length === 0, "cron: сбой refresh " + faults.join("|"));
    MODULES.delete("cron");
  }

  // ---- bots: опрос реплики коуча стоит у скрытого раздела и продолжается в onShow
  {
    let serverBusy = true, editorOnInput = null;
    const bot = {file: "bots/a.md", meta: {name: "A", mcp: [], skills: [], attachments: []}, body: "промпт",
                 problems: [], schedule: null};
    const c = mkctx("bots", (p) => {
      if (p.startsWith("/api/bots/coach")) return {messages: [], busy: serverBusy};
      if (p.startsWith("/api/bots/file")) return bot;
      if (p.startsWith("/api/bots?")) return {bots: [bot], mcp: [], skills: [], presets: []};
      return {};
    });
    c.ui.editor = async (host, text, opts) => { editorOnInput = opts.onInput; return {getValue: () => text}; };
    ns.bots.mount(c); register("bots", ns.bots);
    S.view = "bots";
    await dispatchModules("data"); await tick();
    check(apiCount(c, "/api/bots?") === 1, "bots на виду: список прочитан, а " + apiCount(c, "/api/bots?"));
    const coachBtn = ELS.filter(e => e.attrs["data-llm"] === "coach").pop();
    check(!!coachBtn, "bots: нет кнопки коуча в подставном DOM");
    coachBtn.attrs.onclick(); await tick();                     // реплика уже идёт: openCoach → coachWait
    const loads = () => apiCount(c, "/api/bots/coach");
    check(loads() === 1 && pending(5000).length === 1, "bots: коуч занят — один запрос и опрос ждёт: " + loads() + "/" + pending(5000).length);
    await fire(5000);
    check(loads() === 2 && pending(5000).length === 1, "bots: опрос идёт на виду: " + loads());
    ns.bots.onHide(c);
    await fire(5000);                                           // таймер ожидания сработал уже у скрытого раздела
    check(loads() === 2 && pending().length === 0, "bots: после onHide опрос выходит без нового coachLoad: " + loads() + "/" + pending().length);
    for (let i = 0; i < 3; i++) await fire();
    check(loads() === 2, "bots скрыт: опроса нет вовсе");
    // раздел вернули: опрос продолжается, пока реплика идёт
    ns.bots.onShow(c); await tick();
    check(pending(5000).length === 1, "bots onShow: опрос продолжен, пока реплика идёт");
    await fire(5000);
    check(loads() === 3, "bots после onShow: опрос ходит снова: " + loads());
    // быстрое «ушли и вернулись» не плодит два опроса
    ns.bots.onHide(c); ns.bots.onShow(c); await tick();
    await fire(5000);
    check(loads() === 4 && pending(5000).length === 1, "bots: ушли-вернулись — один опрос, а запросов " + loads() + ", таймеров " + pending(5000).length);
    // ответ лёг: опрос заканчивается, на следующий onShow ничего не возобновляется
    serverBusy = false;
    await fire(5000);
    check(pending().length === 0, "bots: ответ готов — опрос закончен");
    const done = loads();
    ns.bots.onHide(c); ns.bots.onShow(c); await tick(); await fire();
    check(loads() === done && pending().length === 0, "bots: опрос закончен, onShow его не возобновляет");
    // dispose гасит отложенную проверку правки
    editorOnInput();
    check(pending(350).length === 1, "bots: правка ставит отложенную проверку");
    ns.bots.dispose(c);
    check(pending().length === 0, "bots dispose: отложенная проверка снята");
    // скрытый раздел не читает список на «data»
    S.view = "other"; c.calls.length = 0;
    await dispatchModules("data");
    check(c.calls.length === 0, "bots скрыт: «data» не ходит на /api/bots: " + c.calls.join());
    check(faults.length === 0, "bots: сбой refresh " + faults.join("|"));
  }
  console.log(JSON.stringify({fails}));
})().catch(e => { fails.push("исключение: " + (e && e.stack || e)); console.log(JSON.stringify({fails})); });
"""


@test
def test_noisy_sections_are_lazy_and_stop_their_timers(tmp: Path):
    """«Боты», «Cron», «Модели», «О проекте» объявили `lifecycle = 1`: скрытый раздел на «data» не ходит на свой /api
    (счётчик запросов 0) и не стирает несохранённую правку формы «Моделей»; таймер слежения «Cron» гаснет в onHide, не
    взводится ответом, пришедшим после ухода, и поднимается в onShow; опрос реплики коуча «Ботов» выходит после onHide
    без нового запроса и продолжается в onShow, пока реплика идёт (без дублей при «ушли и вернулись»); dispose снимает
    таймеры. Реальные view.js под node с подставным DOM и таймерами (node нет — проверка пропускается)."""
    for mid in ("bots", "cron", "models", "about"):
        text = (KIT / "cockpit" / "modules" / mid / "view.js").read_text(encoding="utf-8")
        assert "export const lifecycle = 1;" in text, mid
    for mid in ("cron", "bots"):
        text = (KIT / "cockpit" / "modules" / mid / "view.js").read_text(encoding="utf-8")
        assert "export function onHide(" in text and "export function onShow(" in text and "export function dispose(" in text, mid
    node = shutil.which("node")
    if not node:
        return
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")

    def cut(start: str, end: str) -> str:
        a = panel.index(start)
        return panel[a:panel.index(end, a)]
    views = tmp / "views"
    views.mkdir()
    for mid in ("bots", "cron", "models", "about"):
        shutil.copy2(KIT / "cockpit" / "modules" / mid / "view.js", views / f"{mid}.mjs")
    code = (NOISY_HARNESS.replace("@MODULES@", cut("const MODULES = new Map();", "function moduleTitle"))
            .replace("@DISPATCH@", cut("async function dispatchModules", "// Подписи разделов в меню"))
            .replace("@DIR@", json.dumps(str(views))))
    src = tmp / "noisy_sections.mjs"
    src.write_text(code, encoding="utf-8")
    import subprocess
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert result["fails"] == [], "\n".join(result["fails"])


# ------------------------------------------------------------------ остальные разделы и охранный тест (BL-ARC-11, срез 3)

# Сколько разделов кита вправе остаться без `lifecycle = 1` (с пометкой «legacy» и причиной). Храповик: число не
# растёт; стало меньше — понизь его. Имён разделов здесь нет: перечень читается из cockpit/modules/*/view.js.
LEGACY_MAX = 1


def _module_views() -> dict[str, str]:
    """{id раздела: текст view.js} — перечень читается с диска, а не списком в тесте."""
    return {p.parent.name: p.read_text(encoding="utf-8")
            for p in sorted((KIT / "cockpit" / "modules").glob("*/view.js"))}


def _declared_life(text: str) -> tuple[bool, str]:
    """(есть `export const lifecycle = 1`, причина пометки `// lifecycle: legacy — …` в первых 20 строках)."""
    import re
    flag = bool(re.search(r"^export const lifecycle\s*=\s*1\s*;", text, re.M))
    head = "\n".join(text.splitlines()[:20])
    m = re.search(r"^//\s*lifecycle:\s*legacy\s*[—-]\s*(\S.*)$", head, re.M)
    return flag, (m.group(1).strip() if m else "")


@test
def test_every_module_declares_lifecycle(_t):
    """Каждый раздел панели объявил жизнь: `export const lifecycle = 1` либо строка `// lifecycle: legacy — <причина>` в
    первых 20 строках view.js (но не оба сразу); разделов с пометкой «legacy» не больше LEGACY_MAX, и они не растут."""
    views = _module_views()
    assert views, "разделов панели не нашли"
    legacy = []
    for mid, text in sorted(views.items()):
        flag, reason = _declared_life(text)
        assert flag or reason, f"{mid}: нет ни `export const lifecycle = 1`, ни `// lifecycle: legacy — <причина>` в первых 20 строках"
        assert not (flag and reason), f"{mid}: и lifecycle, и пометка legacy — оставь одно"
        if reason:
            legacy.append(mid)
    assert len(legacy) <= LEGACY_MAX, f"разделов legacy стало больше планки {LEGACY_MAX}: {legacy}"
    if len(legacy) < LEGACY_MAX:
        print(f"подсказка: legacy-разделов {len(legacy)}, планка LEGACY_MAX = {LEGACY_MAX} — понизь её")


SECTIONS_PRELUDE = NOISY_HARNESS.split("\n(async () => {\n", 1)[0]

SECTIONS_BODY = r"""
// Подставные браузерные глобали для graph/work/gitsync: top-level кода у разделов почти нет.
const dom = {};
const $q = (sel) => dom[sel] || (dom[sel] = node());
function evnode(sel){
  const n = node(); const h = {};
  n.addEventListener = (ev, fn) => { h[ev] = fn; };
  n.fire = (ev, e) => h[ev] && h[ev](e || {});
  dom[sel] = n; return n;
}
let cyMade = 0, cyAlive = 0;
let cyLast = null;
globalThis.cytoscape = (opts) => {
  cyMade++; cyAlive++;
  const cy = {destroyed: false, destroy(){ this.destroyed = true; cyAlive--; }, on(){}, fit(){}, style(){},
              batch(f){ f(); }, z: 1, p: {x: 0, y: 0}, opts, zoom(){ return this.z; }, pan(){ return this.p; },
              viewport(v){ this.z = v.zoom; this.p = v.pan; this.vp = v; }};
  // узлы: позиция берётся из preset-раскладки, а при случайной (cose) каждый раз иная
  const pre = opts.layout && opts.layout.name === "preset";
  const pos = {};
  const list = (opts.elements || []).filter(e => !e.data.source).map(e => ({
    id: () => e.data.id, data: () => e.data,
    position: () => pos[e.data.id]}));
  list.forEach(n => { const id = n.id(); pos[id] = pre ? opts.layout.positions({id: () => id}) : {x: Math.random() * 1000, y: Math.random() * 1000}; });
  cy.nodes = () => list;
  cyLast = cy;
  return cy;
};
globalThis.window = globalThis;
globalThis.getComputedStyle = () => ({getPropertyValue: () => ""});
globalThis.document = {body: {}, head: {append(){}}, createTextNode: (x) => x, createElement: () => node()};
const scriptEl = (tag, attrs, ...kids) => tag === "script"
  ? {set onload(f){ Promise.resolve().then(f); }, set onerror(f){}}
  : el(tag, attrs, ...kids);
const baseCtx = (id, impl, extra) => mkctx(id, impl, {$: $q, el: scriptEl, scenarios: async () => ({scenarios: []}),
  ui: {kbInfo: () => ({at: 0, stale: false, title: ""}), stampText: (x) => x}, on: (e, f) => moduleOn(id, e, f), ...(extra || {})});
async function loadView(id){ return import(pathToFileURL(@DIR@ + "/modules/" + id + "/view.js").href); }
function gate(){ let release; const p = new Promise(r => { release = r; }); return {p, release}; }

(async () => {
  const ids = @IDS@;                      // разделы с lifecycle = 1 (их перечень прислал тест)
  const ns = {};
  for (const id of ids){
    ns[id] = await loadView(id);
    check(ns[id].lifecycle === 1, id + ": нет `export const lifecycle = 1`");
  }

  // ---- каждый раздел с lifecycle: скрытому «data» не зовёт refresh, видимому — ровно один раз; N синтетических
  {
    const counts = {};
    const put = (id, nsx, mod) => MODULES.set(id, {manifest: {id}, mod, ns: nsx, section: node(), subs: null, stale: false, shown: false, hooks: null});
    for (const id of ids){ counts[id] = 0; put(id, ns[id], {refresh: async () => { counts[id]++; }}); }
    const N = 10;
    for (let i = 0; i < N; i++){            // синтетика: половина объявляет lifecycle именованным экспортом, половина — в default
      const id = "synthetic" + i; counts[id] = 0;
      i % 2 ? put(id, {lifecycle: 1}, {refresh: async () => { counts[id]++; }})
            : put(id, null, {lifecycle: 1, refresh: async () => { counts[id]++; }});
    }
    S.view = "none";
    await dispatchModules("data");
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const stale = [...MODULES.values()].filter(r => r.stale).length;
    check(total === 0, "скрытые разделы: «data» дало " + total + " вызовов refresh: " + JSON.stringify(counts));
    check(stale === ids.length + N, "скрытые разделы: stale у " + stale + " из " + (ids.length + N));
    for (const id of ids){
      S.view = id; await dispatchModules("data");
      const others = Object.entries(counts).filter(([k, v]) => k !== id && v !== 0);
      check(counts[id] === 1 && !others.length, id + ": на виду ровно один refresh, а " + counts[id] + "; чужие: " + others.join());
      counts[id] = 0;
    }
    for (const id of [...MODULES.keys()]) MODULES.delete(id);
  }

  // ---- graph: onHide уничтожает cytoscape и снимает таймер подгонки, onShow собирает граф заново
  {
    const graph = {nodes: [{id: "a", title: "A", path: "a.md"}, {id: "b", title: "B", path: "b.md"}],
                   edges: [{from: "a", to: "b"}], orphans: 0, when: 0};
    const c = baseCtx("graph", () => graph);
    $q("#graphDepth").value = "2";
    evnode("#graphFind");
    register("graph", ns.graph);
    ns.graph.mount(c);
    S.view = "graph"; timers.length = 0;
    await dispatchModules("data");
    check(apiCount(c, "/api/graph") === 1 && cyMade === 1 && cyAlive === 1, "graph на виду: один запрос и один cytoscape: " + apiCount(c, "/api/graph") + "/" + cyMade);
    check(pending(30).length === 1, "graph: таймер подгонки взведён");
    S.view = "other";
    await ns.graph.onHide(c);
    check(cyAlive === 0, "graph onHide: cytoscape уничтожен (destroy)");
    check(pending(30).length === 0, "graph onHide: таймер подгонки снят");
    await dispatchModules("data"); await dispatchModules("data");
    check(apiCount(c, "/api/graph") === 1 && cyMade === 1, "graph скрыт: «data» ни запроса, ни раскладки");
    check(MODULES.get("graph").stale === true, "graph скрыт: помечен устаревшим");
    S.view = "graph";
    await ns.graph.refresh(c); await ns.graph.onShow(c);
    check(cyMade === 2 && cyAlive === 1, "graph onShow: граф собран заново (одна раскладка): " + cyMade + "/" + cyAlive);
    check(apiCount(c, "/api/graph") === 1, "graph при возврате не перечитывает данные: " + apiCount(c, "/api/graph"));
    check(pending(30).length === 0, "graph при возврате ставит прежний масштаб и не подгоняет заново");
    await ns.graph.onShow(c);
    check(cyMade === 2, "graph: повторный onShow не перекладывает граф");
    // загрузка, ушедшая в фон, граф в скрытом контейнере не рисует: его соберёт onShow
    await ns.graph.onHide(c);
    S.view = "other"; const before = cyMade;
    await ns.graph.refresh(c, {card: "a"});
    check(cyMade === before, "graph: refresh из скрытого раздела не рисует граф");
    S.view = "graph"; await ns.graph.onShow(c);
    check(cyMade === before + 1 && cyAlive === 1, "graph: onShow дорисовал граф после фоновой загрузки");
    check(faults.length === 0, "graph: сбои " + faults.join("|"));
    MODULES.delete("graph");
  }

  // ---- graph: уход и возврат сохраняют картинку (позиции, масштаб, сдвиг), большой граф не просит «всё равно» снова
  {
    const mk = n => ({nodes: Array.from({length: n}, (_, i) => ({id: "n" + i, title: "N" + i, path: "n" + i + ".md"})),
                      edges: [], orphans: n, when: 0});
    for (const size of [5, 905]){
      const graph = mk(size);
      const c = baseCtx("graph", () => graph);
      $q("#graphDepth").value = "2";
      evnode("#graphFind");
      const outBtn = evnode("#graphOut");
      const gns = await import(pathToFileURL(@DIR@ + "/modules/graph/view.js").href + "?k" + size);   // свежий модуль: своё G
      // кнопка «всё равно» создаётся через ctx.el: считаем её появления в журнале ELS
      const warns = () => ELS.filter(e => e.tag === "button" && e.attrs.onclick).length;
      timers.length = 0;
      const base = cyAlive; let seen = warns();
      const warned = () => { const w = warns() > seen; seen = warns(); return w; };
      register("graph", gns);
      gns.mount(c);
      S.view = "graph";
      await dispatchModules("data");
      if (size > 900){
        check(warned() && cyAlive === base, "graph(" + size + "): без «всё равно» граф не строится");
        ELS.filter(e => e.tag === "button" && e.attrs.onclick).pop().attrs.onclick();
      }
      check(cyAlive === base + 1, "graph(" + size + "): граф собран");
      cyLast.viewport({zoom: 2.5, pan: {x: 33, y: -7}});
      const was = Object.fromEntries(cyLast.nodes().map(n => [n.id(), n.position()]));
      warned();
      S.view = "other"; await gns.onHide(c);
      S.view = "graph"; await gns.onShow(c);
      check(cyAlive === base + 1 && cyLast.opts.layout.name === "preset", "graph(" + size + "): возврат строит граф по сохранённым позициям: " + (cyLast.opts.layout || {}).name);
      check(!warned(), "graph(" + size + "): возврат не показывает предупреждение «слишком большой граф»");
      const now = Object.fromEntries(cyLast.nodes().map(n => [n.id(), n.position()]));
      check(JSON.stringify(now) === JSON.stringify(was), "graph(" + size + "): позиции узлов те же");
      check(cyLast.z === 2.5 && cyLast.p.x === 33 && cyLast.p.y === -7, "graph(" + size + "): масштаб и сдвиг те же");
      check(pending(30).length === 0, "graph(" + size + "): подгонку под окно не повторяем");
      // сменили разброс, пока раздел скрыт, — прежняя раскладка не подходит, строим заново
      S.view = "other"; await gns.onHide(c);
      outBtn.fire("click");
      S.view = "graph"; await gns.onShow(c);
      if (size > 900) check(warned(), "graph(" + size + "): после смены разброса «всё равно» спрашивается заново");
      else check(cyLast.opts.layout.name === "cose", "graph(" + size + "): после смены разброса раскладка считается заново");
      S.view = "other"; await gns.onHide(c);
      MODULES.delete("graph");
      for (const k of Object.keys(dom)) delete dom[k];
      timers.length = 0;
    }
    check(faults.length === 0, "graph(keep): сбои " + faults.join("|"));
  }

  // ---- work: onHide гасит отложенный запрос подсказки; задание, дошедшее у скрытого раздела, ждёт возвращения
  {
    const box = {resets: 0, appended: 0, set innerHTML(v){ this.resets++; }, get innerHTML(){ return ""; },
                 append(...a){ this.appended += a.length; }};
    dom["#makeDialog"] = box;
    const area = evnode("#makeIdea"), list = $q("#makeSuggest");
    $q("#makeKind").value = "k";
    let jobGate = null;
    const c = baseCtx("work", (p) => {
      if (p.startsWith("/api/context/suggest")) return {items: [{kind: "file", value: "x.md", label: "x.md"}]};
      if (p.startsWith("/api/run")) return {job: "j1"};
      if (p.startsWith("/api/job")) return jobGate.p.then(() => ({lines: ["Сессия: `s1`", "ответ планировщика"], next: 1, done: true}));
      if (p.startsWith("/api/kinds")) return {kinds: {}};
      return {};
    });
    register("work", ns.work);
    ns.work.mount(c);
    S.view = "work";
    area.value = "@x"; area.selectionStart = 2;
    area.fire("input");
    check(pending(180).length === 1, "work: набор «@» ставит отложенный запрос подсказки");
    await fire(180);
    check(list.hidden === false, "work на виду: подсказки открылись (проверка самой проверки)");
    // уход при висящем таймере
    area.fire("input");
    check(pending(180).length === 1, "work: второй набор снова ставит таймер");
    S.view = "other"; ns.work.onHide(c);
    check(pending(180).length === 0 && list.hidden === true, "work onHide: таймер подсказки снят, список закрыт");
    // уход при запросе в полёте: ответ список не открывает
    S.view = "work"; area.fire("input");
    let hold = gate(); const prev = c.api;
    c.api = async (path, o) => { if (path.startsWith("/api/context/suggest")){ c.calls.push(path); await hold.p; return {items: [{kind: "file", value: "y.md", label: "y.md"}]}; } return prev(path, o); };
    await fire(180);
    S.view = "other"; ns.work.onHide(c); hold.release(); await tick();
    check(list.hidden === true, "work: ответ подсказки, пришедший после onHide, список не открывает");
    c.api = prev;
    // задание: дошло, пока раздел скрыт; результат на месте и не стёрт возвращением
    S.view = "work"; jobGate = gate();
    $q("#makeIdea").value = "идея";
    $q("#makeGo").onclick();
    await tick();
    S.view = "other"; ns.work.onHide(c);
    jobGate.release(); await tick();
    check(box.appended >= 2, "work: результат задания лёг в разметку, пока раздел скрыт: " + box.appended);
    const resets = box.resets, put = box.appended;
    S.view = "work"; await ns.work.refresh(c);
    check(box.resets === resets && box.appended === put, "work: возвращение не стирает результат задания");
    // скрытому «data» не нужен
    S.view = "other"; c.calls.length = 0;
    await dispatchModules("data");
    check(c.calls.length === 0 && MODULES.get("work").stale === true, "work скрыт: «data» без запросов, но с пометкой stale: " + c.calls.join());
    check(faults.length === 0, "work: сбои " + faults.join("|"));
    MODULES.delete("work");
  }

  // ---- ask: идущий вопрос не прерывается уходом; ответ ждёт на карточке, возвращение его не стирает
  {
    const body = {items: [], resets: 0, prepend(x){ this.items.push(x); }, set innerHTML(v){ this.resets++; },
                  get innerHTML(){ return ""; }, querySelectorAll(){ return this.items; }};
    dom["#askBody"] = body;
    $q("#askText").value = "что в базе?"; $q("#askMode").value = "m";
    let jobGate = gate();
    const c = baseCtx("ask", (p) => {
      if (p.startsWith("/api/run")) return {job: "j1"};
      if (p.startsWith("/api/job")) return jobGate.p.then(() => ({lines: ["# Ответ базы", "ответ из карточек", "Разговор: `x/t1.md`"], next: 1, done: true}));
      if (p.startsWith("/api/ask/threads")) return {threads: []};
      if (p.startsWith("/api/agent")) return {error: "нет"};
      return {};
    });
    register("ask", ns.ask);
    ns.ask.mount(c);
    S.view = "ask";
    await ns.ask.refresh(c);                 // как при открытии: разговор «привязывается» к проекту
    $q("#askGo").onclick(); await tick();
    check(body.items.length === 1, "ask: карточка вопроса легла в разговор");
    S.view = "other";
    const h0 = apiCount(c, "/api/ask/threads");
    await dispatchModules("data");
    check(apiCount(c, "/api/ask/threads") === h0, "ask скрыт: «data» не читает историю");
    jobGate.release(); await tick();
    check(body.items[0].a === "ответ из карточек", "ask: ответ дошёл, пока раздел скрыт, и лёг на свою карточку: " + body.items[0].a);
    const resets = body.resets;
    S.view = "ask"; await ns.ask.refresh(c);
    check(body.resets === resets && body.items.length === 1, "ask: возвращение не стирает ответ");
    check($q("#askNew").hidden === false, "ask: открытый разговор после возвращения на месте");
    check(apiCount(c, "/api/ask/threads") > h0, "ask при открытии перечитывает историю");
    check(faults.length === 0, "ask: сбои " + faults.join("|"));
    MODULES.delete("ask");
  }

  // ---- gitsync: скрытому «data» достаётся подписка (отметка «!» в меню), а не полная перерисовка
  {
    let reloads = 0;
    const project = {path: "/p", git_alert: false};
    const c = baseCtx("gitsync", () => ({status: {}, last: {up: {ok: false, trigger: "auto"}}, settings: {}}),
                      {project, reloadHealth: () => { reloads++; }});
    register("gitsync", ns.gitsync);
    ns.gitsync.mount(c);
    S.view = "other";
    await dispatchModules("data");
    check(apiCount(c, "/api/gitsync") === 1 && project.git_alert === true && reloads === 1,
          "gitsync скрыт: подписка выставила отметку (запросов " + apiCount(c, "/api/gitsync") + ", alert " + project.git_alert + ", reload " + reloads + ")");
    check(MODULES.get("gitsync").stale === false, "gitsync: у раздела с подпиской пометка stale не нужна");
    await dispatchModules("data");
    check(reloads === 1, "gitsync: отметка не меняется — здоровье не пересчитывается снова: " + reloads);
    check(faults.length === 0, "gitsync: сбои " + faults.join("|"));
    MODULES.delete("gitsync");
  }

  // ---- gitsync: проект сменили, пока шёл запрос, — отметка и пересчёт здоровья чужому проекту не достаются
  {
    let reloads = 0;
    const first = {path: "/p", git_alert: false}, second = {path: "/q", git_alert: false};
    const g = gate();
    const c = baseCtx("gitsync", async () => { await g.p; return {status: {}, last: {up: {ok: false, trigger: "auto"}}, settings: {}}; },
                      {project: first, reloadHealth: () => { reloads++; }});
    register("gitsync", ns.gitsync);
    ns.gitsync.mount(c);
    S.view = "other";
    const run = dispatchModules("data");
    c.project = second;                       // выбрали другой проект, пока git-подпроцесс отвечает
    g.release(); await run;
    check(first.git_alert === false && second.git_alert === false && reloads === 0,
          "gitsync: ответ про прежний проект ничего не ставит новому (alert " + second.git_alert + ", reload " + reloads + ")");
    c.project = {path: "/q", git_alert: false};   // ядро подменяет объект проекта при обновлении состояния — путь тот же
    await dispatchModules("data");
    check(c.project.git_alert === true && reloads === 1, "gitsync: тот же проект (новый объект) отметку получает");
    check(faults.length === 0, "gitsync: сбои (смена проекта) " + faults.join("|"));
    MODULES.delete("gitsync");
  }
  console.log(JSON.stringify({fails}));
})().catch(e => { fails.push("исключение: " + (e && e.stack || e)); console.log(JSON.stringify({fails})); });
"""


@test
def test_remaining_sections_are_lazy_and_release_what_they_hold(tmp: Path):
    """Остальные разделы панели ленивы: у каждого с `lifecycle = 1` скрытому «data» даёт 0 вызовов refresh и пометку
    stale (и на 10 синтетических тоже), видимому — один вызов; «Граф» уничтожает cytoscape и снимает таймер подгонки в
    onHide и собирает граф в onShow (из фоновой загрузки граф не рисуется); «Продуктивность» гасит таймер подсказок и не
    открывает список ответом, пришедшим после ухода, а результат задания ждёт возвращения; «Спросить» не стирает
    ответ, дошедший у скрытого раздела; «Git» ведёт отметку в меню подпиской на «data». Реальные view.js под node с
    подставным DOM и таймерами (node нет — проверка пропускается)."""
    views = _module_views()
    lazy = sorted(mid for mid, text in views.items() if _declared_life(text)[0])
    for mid in ("commands", "dev", "graph", "version", "reference", "quickstart", "health", "mirrors", "ask", "work", "gitsync"):
        assert mid in lazy, f"{mid}: нет `export const lifecycle = 1`"
    reports = views["reports"]
    assert not _declared_life(reports)[0] and _declared_life(reports)[1], \
        "«Отчёты» остаются legacy: refresh сохраняет копию отчёта на диске"
    assert "export function onHide(" in views["graph"] and "export function onShow(" in views["graph"]
    assert "export function onHide(" in views["work"]
    assert 'ctx.on("data"' in views["gitsync"]
    node = shutil.which("node")
    if not node:
        return
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")

    def cut(start: str, end: str) -> str:
        a = panel.index(start)
        return panel[a:panel.index(end, a)]
    root = tmp / "kit"
    for mid in views:
        dst = root / "modules" / mid
        dst.mkdir(parents=True)
        for f in (KIT / "cockpit" / "modules" / mid).glob("*.js"):
            shutil.copy2(f, dst / f.name)
    (root / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    code = (SECTIONS_PRELUDE + SECTIONS_BODY)
    code = (code.replace("@MODULES@", cut("const MODULES = new Map();", "function moduleTitle"))
            .replace("@DISPATCH@", cut("async function dispatchModules", "// Подписи разделов в меню"))
            .replace("@DIR@", json.dumps(str(root))).replace("@IDS@", json.dumps(lazy)))
    src = root / "sections.mjs"
    src.write_text(code, encoding="utf-8")
    import subprocess
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert result["fails"] == [], "\n".join(result["fails"])


# ----------------------------------------------- BL-ARC-04, срез 1: профили рабочего места


def _with_profiles(ck, tmp: Path, presets: dict, choice=None):
    """Подменить папку пресетов и файл выбора на временные; вернуть (откат, путь файла выбора).

    Настоящий `local/profile.json` кита и домашняя папка не трогаются: подмена проверяется.
    """
    folder, chosen = tmp / "profiles", tmp / "local" / "profile.json"
    assert str(folder).startswith(str(tmp)) and chosen != Path(ck.PROFILE_FILE)
    for name, data in presets.items():
        _put(folder / name, data)
    folder.mkdir(parents=True, exist_ok=True)
    if choice is not None:
        _put(chosen, choice)
    keep = (ck.PROFILES_DIR, ck.PROFILE_FILE)
    ck.PROFILES_DIR, ck.PROFILE_FILE = str(folder), str(chosen)

    def undo():
        ck.PROFILES_DIR, ck.PROFILE_FILE = keep
    return undo, chosen


@test
def test_profile_all_keeps_the_scenario_list_as_before(tmp: Path):
    """Профиль «all» (файла выбора нет) даёт ровно прежний список сценариев — без предупреждений."""
    ck = _ck()
    undo, _ = _with_profiles(ck, tmp, {})
    try:
        act = ck.active_profile()
        assert act["id"] == "all" and act["warn"] == [] and act["scenarios"] is None, act
        full = ck.all_scenarios()
        assert full and [r["id"] for r in ck.scenarios()] == [r["id"] for r in full]
        assert ck.scenarios() == full
        got = ck.profiles_state("en")
        assert got["active"] == "all" and got["warn"] == [], got
        assert got["profiles"] == [{"id": "all", "name": "Everything",
                                    "about": "All panel sections and routes of the kit"}], got
        assert ck.profiles_state("ru")["profiles"][0]["name"] == "Всё"
    finally:
        undo()


@test
def test_profile_preset_cuts_routes_but_not_the_runners(tmp: Path):
    """Пресет со списком `scenarios` режет то, что видно; запуск (маршрут по id, проверка Cron) профилем не ограничен."""
    ck = _ck()
    import route_runner as RR
    full = [r["id"] for r in ck.all_scenarios()]
    assert len(full) >= 3, full
    pick = [full[2], full[0]]                 # порядок в пресете не важен: остаётся порядок файла сценариев
    undo, _ = _with_profiles(ck, tmp, {
        "role.json": {"id": "role", "name": {"ru": "Роль", "en": "Role"}, "scenarios": pick,
                      "modules": ["work"]}}, choice={"id": "role"})
    try:
        act = ck.active_profile()
        assert act["id"] == "role" and act["warn"] == [] and act["modules"] == ["work"], act
        assert [r["id"] for r in ck.scenarios()] == [i for i in full if i in pick]
        assert [r["id"] for r in ck.scenarios(all_routes=True)] == full
        assert RR.scenario(ck, full[1])["id"] == full[1], "скрытый профилем маршрут запускается"
        assert [r["id"] for r in RR.all_routes(ck)] == full
        got = ck.profiles_state("en")
        assert [p["id"] for p in got["profiles"]] == ["all", "role"] and got["active"] == "role", got
        assert got["profiles"][1]["name"] == "Role" and got["warn"] == [], got
    finally:
        undo()


@test
def test_profile_choice_that_is_broken_falls_back_to_all(tmp: Path):
    """Битый local/profile.json, без id, неизвестный id, отвергнутый пресет — «all» без исключения, причина в warn."""
    ck = _ck()
    full = [r["id"] for r in ck.all_scenarios()]
    cases = {
        "not json": "{ не json",
        "root is a list": "[1, 2]",
        "no id": {"name": "x"},
        "id is a number": {"id": 5},
        "unknown id": {"id": "ghost"},
        "preset rejected": {"id": "bad"},
        "preset with another id": {"id": "liar"},
    }
    for label, choice in cases.items():
        sub = tmp / label.replace(" ", "_")
        undo, _ = _with_profiles(ck, sub, {"bad.json": "{ не json",
                                           "liar.json": {"id": "other", "name": "L"}}, choice=choice)
        try:
            act = ck.active_profile()
            assert act["id"] == "all" and act["warn"], (label, act)
            assert [r["id"] for r in ck.scenarios()] == full, label
        finally:
            undo()
    undo, chosen = _with_profiles(ck, tmp / "gone", {}, choice="x")
    try:
        chosen.unlink()
        assert ck.active_profile()["warn"] == [], "нет файла выбора — обычное состояние, не жалоба"
    finally:
        undo()


@test
def test_profile_manifest_is_checked_by_the_common_loader(tmp: Path):
    """Вид `profile` проверяет общий загрузчик: нет name — отказ; кривой тип списка отброшен с отчётом;
    лишнее в списках не попадает в профиль; `all.json` имя «all» не перебивает; битый пресет не прячет соседей."""
    from aurora_common import load_manifest
    m, rep = load_manifest(_put(tmp / "a.json", {"id": "a"}), "profile", lenient=True)
    assert m is None and any("name" in r for r in rep), rep
    m, rep = load_manifest(_put(tmp / "b.json", {"id": "b", "name": "B", "modules": "work",
                                                 "scenarios": {"x": 1}}), "profile", lenient=True)
    assert m is not None and "modules" not in m and "scenarios" not in m and len(rep) == 2, (m, rep)
    m, rep = load_manifest(_put(tmp / "c.json", {"id": "c", "name": "C", "api_version": 99}),
                           "profile", lenient=True)
    assert m is None and rep, rep
    ck = _ck()
    undo, _ = _with_profiles(ck, tmp / "set", {
        "all.json": {"id": "all", "name": "Подмена"},
        "bad.json": "{ не json",
        "typed.json": {"id": "typed", "name": "T", "modules": "work", "scenarios": ["fix", 7, ""]},
        "fine.json": {"id": "fine", "name": {"ru": "Хороший"}, "about": "o"}})
    try:
        listed = ck.profiles()
        rows = {r["id"]: r for r in listed[1:]}
        assert set(rows) == {"all", "bad", "typed", "fine"} and "error" in rows["bad"], sorted(rows)
        assert "name" in listed[0] and listed[0]["name"] == "Всё" and "error" in rows["all"], \
            "профиль «all» задан кодом и файлом не заменяется"
        assert rows["typed"]["modules"] is None and rows["typed"]["scenarios"] == ["fix"], rows["typed"]
        assert len(rows["typed"]["warn"]) == 2, rows["typed"]
        got = ck.profiles_state("en")
        assert [p["id"] for p in got["profiles"]] == ["all", "fine", "typed"], got
        assert got["profiles"][1]["name"] == "Хороший", "нет перевода — русское имя, а не пустота"
        assert len(got["warn"]) == 4, got["warn"]
    finally:
        undo()


def _hide_all_but_first(ck, tmp: Path):
    """Профиль «role» с одним видимым маршрутом; вернуть (откат, id всех, id скрытого)."""
    full = [r["id"] for r in ck.all_scenarios()]
    assert len(full) >= 3, full
    undo, _ = _with_profiles(ck, tmp, {
        "role.json": {"id": "role", "name": "Роль", "scenarios": [full[0]]}}, choice={"id": "role"})
    return undo, full, full[1]


@test
def test_profile_hides_routes_only_in_the_view_not_in_cron_and_terminal(tmp: Path):
    """Расписание Cron и `aurora.py route` берут все маршруты кита: профиль скрывает, но не запрещает.

    Скрытый маршрут проходит проверку задания (`clean_task`); несуществующий — `bad_route`; ответ
    терминала на незнакомый маршрут перечисляет все маршруты кита, а не только видимые."""
    ck = _ck()
    import cron
    import route_runner as RR
    undo, full, hidden = _hide_all_but_first(ck, tmp)
    try:
        assert [r["id"] for r in ck.scenarios()] == [full[0]]
        projects = [{"name": "A", "path": "/p/a"}]

        def step(route):
            return {"name": "N", "time": "20:00", "steps": [{"kind": "route", "project": "*", "route": route}]}
        task, err = cron.clean_task(ck, step(hidden), projects)
        assert not err and task["steps"][0]["route"] == hidden, (task, err)
        assert cron.clean_task(ck, step("no-such-route"), projects)[1] == "bad_route"
        proj = tmp / "proj"
        proj.mkdir()
        (proj / "aurora.config.yaml").write_text("project: demo\n", encoding="utf-8")
        err_out = io.StringIO()
        with contextlib.redirect_stderr(err_out):
            rc = RR.main([str(proj), "no-such-route"])
        assert rc == 2, rc
        listed = err_out.getvalue().split("Есть:")[1].replace("\n", "").split(", ")
        assert [x.strip() for x in listed] == full, listed
    finally:
        undo()


@test
def test_profile_hidden_routes_stay_reachable_for_the_panel_and_cron_editor(tmp: Path):
    """`/api/cron` отдаёт редактору все маршруты; `/api/scenarios` — видимые, а с `all=1` — все,
    скрытые с `hidden: true` (кнопки по зашитому id и подписи истории ищут по полному списку)."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    ck = _ck()
    undo, full, hidden = _hide_all_but_first(ck, tmp)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp)]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
        c.request("GET", path, headers={"X-Aurora-Token": ck.TOKEN})
        r = c.getresponse()
        return json.loads(r.read().decode("utf-8"))
    try:
        assert [r["id"] for r in get("/api/scenarios")["scenarios"]] == [full[0]]
        rows = get("/api/scenarios?all=1")["scenarios"]
        assert [r["id"] for r in rows] == full, "в полном списке порядок и состав файла сценариев"
        assert [r["id"] for r in rows if r.get("hidden")] == [i for i in full if i != full[0]]
        assert [r["id"] for r in get("/api/cron")["routes"]] == full, "редактор Cron видит все маршруты"
    finally:
        srv.shutdown()
        undo()
    ui = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert '"/api/scenarios"' not in ui, "панель ищет маршрут по урезанному списку профиля"
    routes = (KIT / "cockpit" / "modules" / "quickstart" / "routes.js").read_text(encoding="utf-8")
    assert "!sc.hidden" in routes, "Быстрый старт показывает и скрытые профилем маршруты"


@test
def test_profile_warnings_are_not_doubled_and_empty_fields_do_not_hide_everything(tmp: Path):
    """Активный пресет с одним предупреждением даёт его один раз; список, где отброшено всё, —
    «поля нет» (видно всё), а не «скрыть всё»; пустое name — отказ пресета с причиной."""
    ck = _ck()
    full = [r["id"] for r in ck.all_scenarios()]
    undo, _ = _with_profiles(ck, tmp, {
        "once.json": {"id": "once", "name": "Один", "modules": ["work", 7], "scenarios": [full[0]]},
        "junk.json": {"id": "junk", "name": "Мусор", "scenarios": [7, ""], "modules": ["", None]},
        "blank.json": {"id": "blank", "name": ""},
        "empty.json": {"id": "empty", "name": "Пусто", "scenarios": []}}, choice={"id": "once"})
    try:
        got = ck.profiles_state("ru")
        mine = [w for w in got["warn"] if "«once»" in w]
        assert len(mine) == 1 and "modules" in mine[0], got["warn"]
        rows = {r["id"]: r for r in ck.profiles()[1:]}
        assert rows["junk"]["scenarios"] is None and rows["junk"]["modules"] is None, rows["junk"]
        assert len(rows["junk"]["warn"]) == 2, rows["junk"]
        assert "error" in rows["blank"] and "name" in rows["blank"]["error"], rows["blank"]
        assert rows["empty"]["scenarios"] == [], "явный пустой список — скрыть всё, это не ошибка"
        _put(Path(ck.PROFILE_FILE), {"id": "junk"})
        assert ck.active_profile()["scenarios"] is None
        assert [r["id"] for r in ck.scenarios()] == full, "список из мусора не должен прятать маршруты"
    finally:
        undo()


@test
def test_tests_do_not_see_the_owners_profile(tmp: Path):
    """Прогон тестов не читает настоящий `local/profile.json`: выбор роли на машине не меняет `scenarios()`."""
    ck = _ck()
    assert Path(ck.PROFILE_FILE) != KIT / "local" / "profile.json", ck.PROFILE_FILE
    assert not Path(ck.PROFILE_FILE).exists()
    assert [r["id"] for r in ck.scenarios()] == [r["id"] for r in ck.all_scenarios()]


# ----------------------------------------------- BL-ARC-04, срез 2: профиль скрывает разделы панели


def _real_module_ids() -> list:
    """Разделы кита — по папкам `cockpit/modules/*/module.json`; имён в тесте нет."""
    return sorted(p.parent.name for p in (KIT / "cockpit" / "modules").glob("*/module.json"))


@test
def test_profile_without_modules_leaves_the_section_list_as_before(tmp: Path):
    """Профиль «all» и пресет без поля `modules` не меняют `modules()`: ни одна запись не помечена `hidden`,
    состав и порядок те же, что без профиля."""
    ck = _ck()
    ids = _real_module_ids()
    assert len(ids) >= 3, ids
    undo, _ = _with_profiles(ck, tmp, {}, choice=None)
    try:
        base = ck.modules()
        assert sorted(m["id"] for m in base) == ids
        assert not [m for m in base if "hidden" in m], "без профиля прятать нечего"
    finally:
        undo()
    undo, _ = _with_profiles(ck, tmp / "p2", {
        "plain.json": {"id": "plain", "name": "Простой", "scenarios": ["x"]}}, choice={"id": "plain"})
    try:
        assert ck.active_profile()["modules"] is None
        assert ck.modules() == base, "пресет без `modules` даёт тот же ответ, что «all»"
    finally:
        undo()
    undo, _ = _with_profiles(ck, tmp / "p3", {}, choice={"id": "all"})
    try:
        assert ck.modules() == base
    finally:
        undo()


@test
def test_profile_marks_exactly_the_sections_outside_its_list(tmp: Path):
    """Пресет с `modules` помечает `hidden: true` ровно дополнение к списку; остальные записи те же, что без профиля;
    имена разделов берутся из перечня `cockpit/modules`, а не из теста."""
    ck = _ck()
    ids = _real_module_ids()
    keep = [ids[0], ids[2]]
    undo, _ = _with_profiles(ck, tmp / "none", {})
    try:
        base = {m["id"]: m for m in ck.modules()}
    finally:
        undo()
    undo, _ = _with_profiles(ck, tmp, {
        "role.json": {"id": "role", "name": "Роль", "modules": keep + ["no-such-section"]}}, choice={"id": "role"})
    try:
        got = {m["id"]: m for m in ck.modules()}
    finally:
        undo()
    assert sorted(got) == ids, "скрытый раздел остаётся в ответе: панели нужен его манифест"
    assert sorted(i for i, m in got.items() if m.get("hidden")) == sorted(set(ids) - set(keep))
    for i in ids:
        want = dict(base[i], hidden=True) if i not in keep else base[i]
        assert got[i] == want, (i, got[i])
    # явный пустой список — скрыть всё, как у `scenarios`
    undo, _ = _with_profiles(ck, tmp / "empty", {
        "none.json": {"id": "none", "name": "Ничего", "modules": []}}, choice={"id": "none"})
    try:
        assert all(m.get("hidden") for m in ck.modules())
    finally:
        undo()


@test
def test_profile_does_not_hide_broken_sections_and_keeps_their_files(tmp: Path):
    """Запись с `error` профиль не скрывает (поломку видно); файлы скрытого раздела по-прежнему отдаются —
    профиль не механизм прав."""
    ck = _ck()
    root = tmp / "modules"
    _put(root / "fine" / "module.json", {"id": "fine", "name": {"ru": "Годный"}, "group": "project"})
    _put(root / "fine" / "view.js", "export default {};")
    _put(root / "other" / "module.json", {"id": "other", "name": {"ru": "Другой"}})
    _put(root / "junk" / "module.json", "{ не json")
    _put(root / "alien" / "module.json", {"id": "elsewhere", "name": {"ru": "Чужой"}})
    undo, _ = _with_profiles(ck, tmp / "prof", {
        "role.json": {"id": "role", "name": "Роль", "modules": ["other"]}}, choice={"id": "role"})
    keep = ck.MODULES_DIR
    ck.MODULES_DIR = str(root)
    try:
        by = {m["id"]: m for m in ck.modules()}
        assert by["fine"].get("hidden") is True and "hidden" not in by["other"], by
        for bad in ("junk", "alien"):
            assert "error" in by[bad] and "hidden" not in by[bad], by[bad]
        assert ck.module_file("fine", "view.js"), "файл скрытого раздела отдаётся"
    finally:
        ck.MODULES_DIR = keep
        undo()


HIDDEN_HARNESS = r"""
const faults = [], toasts = [], mounted = [], hid = [], buttons = [], warns = [];
console.warn = (...a) => warns.push(a.join(" "));
const NOOP = () => {};
const t = (k, p) => k + ":" + JSON.stringify(p || {});
const toast = (text, kind) => { toasts.push({text, kind}); };
const group = {insertBefore: (b) => buttons.push(b.attrs["data-view"])};
const el = (tag, attrs) => ({tag, attrs, dataset: {}, classList: {toggle: NOOP}});
const $ = (sel) => (sel.startsWith(".navgroup") ? group : null);
const $$ = () => [];
const S = {view: "none", lang: "ru", project: null, state: {}};
const location = {hash: ""};
const MODULES = new Map();
let REPLY = {modules: []};
const api = async () => REPLY;
const confirmLeave = () => true, devOnlyView = () => false, devSectionsOn = () => false;
const hideModule = (id) => hid.push(id), mountModule = (id) => mounted.push(id), refreshActivity = NOOP,
  renderSetup = NOOP, drawLangPicker = NOOP, renderProject = NOOP, renderFiles = NOOP, renderHistory = NOOP,
  loadRuns = NOOP, drawTaskButton = NOOP, drawLiveJobs = () => Promise.resolve(), autoFollow = NOOP, ROUTE = null;
@NAV@
@HIDDEN@
@SHOW@
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
(async () => {
  const m = (id, extra) => Object.assign({id, name: {ru: "Раздел " + id}, group: "project", order: 10, needs: []}, extra || {});
  REPLY = {modules: [m("shown_one"), m("hidden_one", {hidden: true, warn: ["поле"], behind: true}),
                     m("hidden_two", {hidden: true}), {id: "broken_one", error: "плохой"}]};
  await loadModules();
  check(toasts.length === 1 && toasts[0].text.startsWith("modules.broken"),
        "про скрытый раздел с warn нет toast, про сломанный есть: " + JSON.stringify(toasts));
  check(warns.length === 0, "про behind скрытого раздела в консоль не пишем: " + warns.join());
  check(buttons.join() === "shown_one", "кнопка только у видимого раздела: " + buttons.join());
  check(MODULES.has("hidden_one") && MODULES.has("shown_one"), "запись скрытого раздела в MODULES есть");
  check(!MODULES.has("broken_one"), "раздел с error не поднят, как и раньше");
  check(toasts.length === 1 && toasts[0].text.startsWith("modules.broken"), "про сломанный раздел сказано одним сообщением");
  toasts.length = 0;
  show("hidden_one");
  check(S.view === "overview", "скрытый раздел уводит на Мостик, а вид — " + S.view);
  check(!mounted.includes("hidden_one"), "скрытый раздел не монтируется");
  check(toasts.length === 1 && toasts[0].text.startsWith("modules.hidden") && toasts[0].kind === "warn"
        && toasts[0].text.includes("Раздел hidden_one"), "клик, адрес или ссылка в скрытый раздел называют причину: " + JSON.stringify(toasts));
  toasts.length = 0; S.view = "none";
  show("hidden_one", undefined, {quiet: true});
  check(S.view === "overview" && toasts.length === 0, "автопереход (выбор проекта → «Здоровье») уводит на Мостик молча: " + JSON.stringify(toasts));
  S.view = "none";
  show("hidden_two", {card: "x"});
  check(S.view === "overview" && toasts.length === 1 && toasts[0].text.includes("Раздел hidden_two"),
        "переход с грузом тоже говорит причину: " + JSON.stringify(toasts));
  toasts.length = 0;
  show("shown_one");
  check(S.view === "shown_one" && toasts.length === 0, "видимый раздел открывается молча");
  S.view = "none"; mounted.length = 0;
  toasts.length = 0;
  show("shown_one");
  check(S.view === "shown_one" && mounted.join() === "shown_one" && toasts.length === 0, "видимый раздел открывается как раньше");
  show("hidden_one");
  check(hid.includes("shown_one"), "уходящий видимый раздел узнал об уходе");
  check(!hid.includes("hidden_one"), "в скрытый раздел не заходили — и уходить из него нечему: " + hid.join());
  console.log(JSON.stringify({fails}));
})().catch(e => { console.log(JSON.stringify({fails: ["исключение: " + (e && e.stack || e)]})); });
"""


@test
def test_panel_does_not_mount_a_section_hidden_by_the_profile(tmp: Path):
    """Панель (под node, иначе проверка пропускается): у скрытого профилем раздела нет кнопки меню, запись в `MODULES`
    есть; `show` уводит на Мостик с предупреждением из каталога (молчит только автопереход с `quiet`) и не монтирует раздел; видимые открываются как раньше;
    в самом JS нет имён разделов, а строка живёт в каталогах ru и en."""
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    for lang in ("ru", "en"):
        cat = json.loads((KIT / "cockpit" / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "{id}" in cat.get("modules.hidden", ""), f"нет строки modules.hidden в {lang}.json"

    def cut(start: str, end: str, keep_end: bool = False) -> str:
        a = panel.index(start)
        b = panel.index(end, a)
        return panel[a:b + (len(end) if keep_end else 0)]
    nav = cut("function moduleTitle(m){", "// Всё, чем ядро делится")
    hidden = cut("function profileHidden(view){", "function showDevNav")
    show = cut("function show(view, payload, opts){", "\n}\n", keep_end=True)
    assert "manifest.hidden" in hidden and "profileHidden(view)" in show
    assert "modules.hidden" in show and "opts.quiet" in show, "show() говорит причину, молчит только по флагу quiet"
    assert 'show("health", undefined, {quiet: true})' in panel, "молчит единственный автопереход — выбор проекта → «Здоровье»"
    assert "if (!m.hidden) addModuleNav(m)" in nav
    node = shutil.which("node")
    if not node:
        return
    code = HIDDEN_HARNESS.replace("@NAV@", nav).replace("@HIDDEN@", hidden).replace("@SHOW@", show)
    src = tmp / "hidden_panel.mjs"
    src.write_text(code, encoding="utf-8")
    import subprocess
    cp = subprocess.run([node, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    result = json.loads(cp.stdout.strip().splitlines()[-1])
    assert result["fails"] == [], "\n".join(result["fails"])


@test
def test_profile_warns_about_a_module_id_that_is_not_a_section(tmp: Path):
    """Опечатка в `modules` («Work» вместо id раздела) прячет всё: профиль обязан сказать об этом в `warn` —
    у активного профиля и в `/api/profiles`; верные `id` предупреждений не дают, профиль «all» тоже."""
    ck = _ck()
    ids = _real_module_ids()
    undo, _ = _with_profiles(ck, tmp / "typo", {
        "role.json": {"id": "role", "name": "Роль", "modules": [ids[0].upper(), ids[1], "nope", "nope", "files"]},
        "fine.json": {"id": "fine", "name": "Ладный", "modules": [ids[0], ids[1]]}}, choice={"id": "role"})
    try:
        act = ck.active_profile()
        bad = [w for w in act["warn"] if "в modules — не раздел" in w]
        # «nope» назван дважды — строка одна; встроенный «files» назван честно: профиль его не прячет
        assert len(bad) == 3 and any(ids[0].upper() in w for w in bad) and sum("nope" in w for w in bad) == 1, act["warn"]
        assert any("«files»" in w and "встроенные разделы профиль не прячет" in w for w in bad), bad
        assert not any("«%s»" % ids[1] in w for w in bad)
        state = ck.profiles_state("ru")
        assert [w for w in state["warn"] if "в modules — не раздел" in w and "profile «role»" in w] == bad, state["warn"]
        # на английском строка переведена, кириллицы в ней нет (иначе `kit_i18n --check` её не видит)
        en = [w for w in ck.translate_payload(state, "en")["warn"] if "profile «role»" in w and "modules" in w]
        assert len(en) == 3 and not any(re.search("[а-яё]", w, re.I) for w in en), en
        assert any("is not a section from cockpit/modules" in w for w in en), en
        assert not any("profile «fine»" in w for w in state["warn"]), "верный список предупреждений не даёт"
        assert len(state["warn"]) == len(set(state["warn"])), "активный профиль не задваивает строки"
    finally:
        undo()
    undo, _ = _with_profiles(ck, tmp / "none", {})
    try:
        assert ck.active_profile()["warn"] == [] and ck.profiles_state("ru")["warn"] == []
    finally:
        undo()


@test
def test_profile_section_hiding_is_documented(_t):
    """Справочник раздела (ru) и описание панели (en) говорят, что профиль прячет разделы полем `hidden` и что это не права."""
    ru = (KIT / "cockpit" / "modules" / "README.md").read_text(encoding="utf-8")
    en = (KIT / "docs" / "en" / "control-panel.md").read_text(encoding="utf-8")
    pr = (KIT / "cockpit" / "profiles" / "README.md").read_text(encoding="utf-8")
    assert "hidden" in ru and "профил" in ru and "не права" in ru
    assert "hidden" in en and "profile" in en and "not permissions" in en
    ru, en = " ".join(ru.split()), " ".join(en.split())
    assert "молчит только автопереход" in ru and "only the automatic jump" in en, "тихий автопереход описан, клики — нет"
    assert "не раздел из cockpit/modules" in ru and "is not a section" in en, "предупреждение об опечатке описано"
    assert "со следующего выпуска" not in pr, "README профилей ещё обещает разделы «в следующем выпуске»"
