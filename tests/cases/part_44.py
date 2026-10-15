"""Проверки движка Aurora, часть 44. Каркас и помощники — tests/harness.py.

Метрика «файлов ядра на расширение» и храповик (BL-ARC-02, срез 1): `tests/ext_footprint.py`
считает, сколько файлов ядра знают расширения каждой точки (коннектор, раздел панели,
провайдер Git, провайдер LLM, вид артефакта, ассистент) по литералам из самих манифестов и
реестров; число не должно расти относительно `tests/ext_footprint_baseline.json`.
Модель и сеть не используются; копии дерева — только во временных папках.
"""
from __future__ import annotations

import ast
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
    """Файлов ядра на расширение не больше планки: метрика не растёт, снизилась — обновить планку."""
    base = EF.load_baseline()
    assert base, "нет планки tests/ext_footprint_baseline.json — python3 tests/ext_footprint.py --write"
    fp = EF.footprint(KIT)
    for name in EF.ratcheted():
        assert name in base, (name, "нет в планке: python3 tests/ext_footprint.py --write")
        now = len(fp[name])
        assert now <= base[name], (
            f"{name}: файлов ядра {now} > планки {base[name]}; ядро начало знать расширения "
            f"по имени, вынесите литерал в манифест или реестр: {fp[name]}")
        assert now >= base[name], (
            f"{name}: файлов ядра {now} < планки {base[name]} — хорошо; обновите планку: "
            f"python3 tests/ext_footprint.py --write")


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
