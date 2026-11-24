"""Проверки движка Aurora, часть 263. Маршруты панели после переноса производства в «Файлы».

`write` и `deliver` больше не маршруты: артефакт создаётся карточкой «Файлов», выпуск
делает карточка существующего документа. Остались три маршрута обслуживания базы:
`update`, `fix` и `rebuild`. Команды производства и публикации остаются в реестре.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401

COCKPIT = KIT / "cockpit"
I18N = COCKPIT / "i18n" / "data" / "en.json"
REMOVED = ("write", "deliver")


def _ck():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


def _put(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                    encoding="utf-8")
    return path


def _with_profiles(ck, tmp: Path, presets: dict, choice=None, enabled: bool = True):
    """Подменить папку пресетов, файл выбора и файл флагов на временные; вернуть откат."""
    folder = tmp / "profiles"
    chosen = tmp / "local" / "profile.json"
    flags = tmp / "local" / "features.json"
    for name, data in presets.items():
        _put(folder / name, data)
    folder.mkdir(parents=True, exist_ok=True)
    if choice is not None:
        _put(chosen, choice)
    if enabled:
        _put(flags, {"profiles": True})
    keep = (ck.PROFILES_DIR, ck.PROFILE_FILE, ck.FEATURES_FILE)
    ck.PROFILES_DIR, ck.PROFILE_FILE, ck.FEATURES_FILE = str(folder), str(chosen), str(flags)

    def undo():
        ck.PROFILES_DIR, ck.PROFILE_FILE, ck.FEATURES_FILE = keep
    return undo


@test
def test_exact_routes_are_update_fix_rebuild(tmp: Path):
    """В `scenarios.txt` остались ровно три маршрута базы: update, fix, rebuild.

    `write` и `deliver` удалены целиком, а не спрятаны: у них не должно остаться ни
    заголовка, ни шагов, которые прилипли бы к предыдущему маршруту.
    """
    ck = _ck()
    rows = ck.all_scenarios()
    assert [r["id"] for r in rows] == ["update", "fix", "rebuild"], rows
    assert {r["group"] for r in rows} == {"база"}, [(r["id"], r["group"]) for r in rows]
    text = (COCKPIT / "scenarios.txt").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\[(?:write|deliver)\]", text), "удалённый маршрут остался заголовком"
    for gone in REMOVED:
        assert gone not in {r["id"] for r in rows}


@test
def test_scenarios_and_payload_drop_removed_routes(tmp: Path):
    """`scenarios()` профиля «all» и `/api/scenarios` отдают те же три маршрута без write/deliver."""
    ck = _ck()
    undo = _with_profiles(ck, tmp, {})
    try:
        visible = ck.scenarios()
        assert [r["id"] for r in visible] == ["update", "fix", "rebuild"], visible
        payload = ck.scenarios_payload("en")
        assert [r["id"] for r in payload] == ["update", "fix", "rebuild"], payload
        assert not ({r["id"] for r in payload} & set(REMOVED))
        assert all(r["title"] and r["title"] != r["id"] for r in payload), payload
    finally:
        undo()


@test
def test_only_rebuild_is_destructive(tmp: Path):
    """Пометку «опасный» несёт только «Пересобрать базу с нуля»."""
    ck = _ck()
    flags = {s["id"]: s["destructive"] for s in ck.all_scenarios()}
    assert flags == {"update": False, "fix": False, "rebuild": True}, flags
    head = next(ln for ln in (COCKPIT / "scenarios.txt").read_text(encoding="utf-8").splitlines()
                if ln.startswith("[rebuild]"))
    assert head.rstrip().endswith("| опасный"), head[-80:]


@test
def test_route_step_keys_stay_unique(tmp: Path):
    """Ключи «сделано» шагов оставшихся маршрутов уникальны: повтор подписи получает позицию."""
    import route_runner as RR

    class _Panel:
        def command_by_name(self, name):
            return {"cmd": name, "runnable": True}

    ck = _ck()
    for sc in ck.all_scenarios():
        steps = RR.plan(_Panel(), sc, True)
        assert steps, sc["id"]
        sigs = [(s["cmd"] + " " + " ".join(s["args"])).strip() for s in steps]
        keys = [f"{sig} @{st['pos']}" if sigs.count(sig) > 1 else sig
                for st, sig in zip(steps, sigs)]
        assert len(keys) == len(set(keys)), \
            (sc["id"], sorted(k for k in keys if keys.count(k) > 1))


@test
def test_scenario_i18n_has_no_orphans(tmp: Path):
    """В `en.json` не осталось переводов заголовков и шагов удалённых маршрутов."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import kit_i18n as KI
    need = KI.scenario_texts()
    data = json.loads(I18N.read_text(encoding="utf-8"))
    have = {k for k, v in (data.get("scenarios") or {}).items() if str(v).strip()}
    assert not (have - need), f"лишние тексты маршрутов: {sorted(have - need)}"
    assert not (need - have), f"нет перевода: {sorted(need - have)}"
    for gone in ("Написать артефакт", "Сдать документы", "Написать содержание",
                 "Сверить основания", "собрать поставляемое из базы, проверить, выгрузить "
                 "и заморозить переданную версию"):
        assert gone not in have, gone


@test
def test_profile_with_removed_write_warns_not_raises(tmp: Path):
    """Старый пресет со списком `["write"]` не роняет панель: предупреждение и пустой список."""
    ck = _ck()
    undo = _with_profiles(ck, tmp, {
        "role.json": {"id": "role", "name": "Role", "scenarios": ["write"]}}, choice={"id": "role"})
    try:
        act = ck.active_profile()
        assert act["id"] == "role", act
        assert any("write" in w and "не маршрут" in w for w in act["warn"]), act["warn"]
        assert ck.scenarios() == [], "профиль показал маршрут, которого больше нет"
        state = ck.profiles_state("en")
        assert any("write" in w for w in state["warn"]), state["warn"]
    finally:
        undo()


@test
def test_shipped_profiles_do_not_name_removed_routes(tmp: Path):
    """Ни один пресет кита не просит write/deliver, а все его маршруты существуют."""
    ck = _ck()
    known = {r["id"] for r in ck.all_scenarios()}
    for path in sorted((COCKPIT / "profiles").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        picked = raw.get("scenarios")
        if picked is None:
            continue
        assert picked, path.name
        assert not (set(picked) & set(REMOVED)), (path.name, picked)
        assert set(picked) <= known, (path.name, sorted(set(picked) - known))


@test
def test_route_ask_and_write_ask_are_not_route_specific(tmp: Path):
    """`route.ask` и `run.write_ask` остаются: их спрашивает запуск любой команды и админский маршрут."""
    ui = (COCKPIT / "ui" / "panel.js").read_text(encoding="utf-8")
    assert 't("route.ask"' in ui and 't("run.write_ask"' in ui
    for lang in ("ru", "en"):
        cat = json.loads((COCKPIT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert cat.get("route.ask"), lang
        assert cat.get("run.write_ask"), lang


@test
def test_commands_doc_has_no_removed_routes(tmp: Path):
    """`docs/commands.md` описывает команды, а не удалённые маршруты."""
    doc = (KIT / "docs" / "commands.md").read_text(encoding="utf-8")
    for gone in ("[write]", "[deliver]", "Написать артефакт", "Сдать документы"):
        assert gone not in doc, gone
    assert "ship:release" in doc and "agent:make" in doc, "команды производства пропали из справочника"
