"""Проверки движка Aurora, часть 84: единый статус версии движка проекта (BL-LCY-01).

Каркас и помощники — tests/harness.py. «Отстал ли движок проекта» было тремя разными вещами: штамп не равен ките
(так «Обновить все» понижало проект, чей движок НОВЕЕ кита), другой минор (гейт останавливал маршруты и ночной Cron
после каждого выпуска) и копия на машине старше. Теперь на это отвечает одна функция `aurora_common.engine_status`
(`ok`, `behind_minor`, `behind_patch`, `ahead`, `partial`, `pinned`, `unknown` и признак «останавливать»), а панель,
гейт, «Обновить все», `kit:doctor` и сам `update` читают её. Сеть и настоящий HOME проверкам не нужны.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import subprocess
import sys
import types

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401


def _mods():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_common"), importlib.import_module("aurora_update"), importlib.import_module("aurora_cockpit")


def _project(tmp: Path, name: str = "proj", stamp: str = "", mark: str = "", lock: "dict | None" = None,
             ignore: str = "") -> Path:
    root = tmp / name
    (root / "AuroraKnowledgeDB" / "meta").mkdir(parents=True)
    (root / ".aurora" / "state").mkdir(parents=True)
    (root / "aurora.config.yaml").write_text('project:\n  name: "PRJ-A"\n  slug: "prj-a"\n', encoding="utf-8")
    if stamp:
        (root / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text(stamp + "\n", encoding="utf-8")
    if mark:
        (root / ".aurora/engine_version.txt").write_text(mark + "\n", encoding="utf-8")
    if lock is not None:
        (root / ".aurora/engine.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    if ignore:
        (root / "aurora.update_ignore.txt").write_text(ignore + "\n", encoding="utf-8")
    return root


@test
def test_engine_status_answers_with_one_definition_of_behind(tmp: Path):
    """Таблица статусов: версии сравниваются числами (1.9 < 1.10), `ahead` — по движку и по штампу, `partial` — по метке
    обновления (пока замок не занят), `pinned` — если проект ведёт ядро сам, `unknown` — без версии."""
    AC, AU, _ck = _mods()
    kit = "1.177.0"
    cases = (
        ("равны", dict(stamp="1.177.0", mark="1.177.0"), "ok"),
        ("метка на минор старше", dict(mark="1.176.9"), "behind_minor"),
        ("отличается минор", dict(mark="1.170.0"), "behind_minor"),
        ("числа, а не строки", dict(mark="1.9.0"), "behind_minor"),
        ("метка новее", dict(stamp="1.177.0", mark="1.180.0"), "ahead"),
        ("штамп новее при старой копии: копия по-прежнему отстала", dict(stamp="1.190.0", mark="1.100.0"), "behind_minor"),
        ("только штамп, старый", dict(stamp="1.150.0"), "behind_minor"),
        ("ни штампа, ни метки", dict(), "unknown"),
        ("проект ведёт ядро сам", dict(mark="1.150.0", ignore=".aurora/scripts/aurora_common.py"), "pinned"),
    )
    for n, (label, kw, want) in enumerate(cases):
        st = AC.engine_status(_project(tmp, f"p{n}", **kw), kit, 1)
        assert st["status"] == want, (label, st)
    stamp_only = AC.engine_status(_project(tmp, "sa", stamp="1.190.0", mark="1.100.0"), kit, 1)
    assert stamp_only["stamp_ahead"] and "штамп проекта 1.190.0 новее" in stamp_only["stamp_reason"], stamp_only
    assert not AC.engine_status(_project(tmp, "sb", stamp="1.150.0", mark="1.170.0"), kit, 1)["stamp_ahead"]
    patch = AC.engine_status(_project(tmp, "pp", mark="1.177.0"), "1.177.4", 1)
    assert patch["status"] == "behind_patch" and not patch["blocks"], patch
    assert AC.version_key("1.10.0") > AC.version_key("1.9.0") and AC.version_key("1.2.3+x") == (1, 2, 3)
    # не равенство объектов: тесты порядка перезагружают aurora_common, и имя в aurora_update держит прежний объект
    assert AU.version_key.__module__ == "aurora_common", "в aurora_update осталась своя копия сравнения версий"
    root = _project(tmp, "pend", mark="1.177.0")
    (root / ".aurora/state/update.pending").write_text('{"from": "1.0.0", "to": "1.177.0"}', encoding="utf-8")
    st = AC.engine_status(root, kit, 1)
    assert st["status"] == "partial" and st["blocks"], st
    import threading
    held, release, taken = threading.Event(), threading.Event(), []

    def update_goes_on():
        lock = importlib.import_module("aurora_fs").FileLock(root / ".aurora/state/update.lock")
        taken.append(lock.acquire(timeout=5))
        held.set()
        release.wait(60)
        lock.release()
    t = threading.Thread(target=update_goes_on)
    t.start()
    try:
        assert held.wait(60) and taken == [True]
        live = AC.engine_status(root, kit, 1)
        assert live["status"] == "updating" and live["blocks"], "идущее обновление не остановило маршрут"
    finally:
        release.set()
        t.join(60)


@test
def test_engine_status_blocks_by_the_engine_contract_not_by_every_release(tmp: Path):
    """Гейт: проект с замком движка (знает свой `ENGINE_API`) не останавливается выпуском нового минора — только ростом
    контракта; проект без замка живёт по прежнему правилу минора; движок новее контракта кита тоже останавливает."""
    AC, _AU, _ck = _mods()
    lock1 = {"version": "1.170.0", "engine_api": 1, "files": {}}
    behind = AC.engine_status(_project(tmp, "a", mark="1.170.0", lock=lock1), "1.177.0", 1)
    assert behind["status"] == "behind_minor" and not behind["blocks"], "выпуск нового минора остановил маршруты и Cron"
    bumped = AC.engine_status(_project(tmp, "b", mark="1.170.0", lock=lock1), "1.177.0", 2)
    assert bumped["blocks"], "контракт кита вырос, а гейт не остановил"
    bumped_patch = AC.engine_status(_project(tmp, "c", mark="1.177.0", lock={**lock1, "version": "1.177.0"}), "1.177.3", 2)
    assert bumped_patch["status"] == "behind_patch" and bumped_patch["blocks"], bumped_patch
    old_world = AC.engine_status(_project(tmp, "d", mark="1.170.0"), "1.177.0", 1)
    assert old_world["blocks"], "проект без замка движка потерял прежнюю защиту минора"
    newer_contract = AC.engine_status(_project(tmp, "e", mark="1.190.0", lock={"version": "1.190.0", "engine_api": 3}), "1.177.0", 1)
    assert newer_contract["status"] == "ahead" and newer_contract["blocks"], newer_contract
    plain_ahead = AC.engine_status(_project(tmp, "f", mark="1.190.0"), "1.177.0", 1)
    assert plain_ahead["status"] == "ahead" and plain_ahead["blocks"], "без замка движка прежнее правило минора держит и «новее»"
    same_minor = AC.engine_status(_project(tmp, "g", mark="1.177.9"), "1.177.0", 1)
    assert same_minor["status"] == "ahead" and not same_minor["blocks"], same_minor
    with_lock = AC.engine_status(_project(tmp, "h", mark="1.190.0", lock={"version": "1.190.0", "engine_api": 1}), "1.177.0", 1)
    assert with_lock["status"] == "ahead" and not with_lock["blocks"], with_lock


@test
def test_the_panel_gate_card_and_update_all_use_the_status(tmp: Path):
    """Панель: `behind` в карточке — по статусу (проект новее кита не «отстал»), гейт `version_gap` говорит по статусу,
    «Обновить все» не понижает `ahead`, не трогает `pinned` и занятый проект и называет их отдельной строкой."""
    AC, AU, ck = _mods()
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "ver != kit_version()" not in src and "minor(ver)" not in src and "def minor(" not in src, \
        "в панели осталось собственное определение «отстал»"
    kit_dir = tmp / "kit"
    kit_dir.mkdir()
    (kit_dir / "VERSION").write_text("1.177.0\n", encoding="utf-8")
    (kit_dir / "ENGINE_API").write_text("2\n", encoding="utf-8")
    saved = (ck.KIT, ck.find_projects, ck.kit_is_source, ck.subprocess.run)
    ck.KIT = str(kit_dir)
    try:
        newer = _project(tmp, "newer", stamp="1.190.0", mark="1.190.0")
        old = _project(tmp, "old", stamp="1.170.0", mark="1.170.0")
        pinned = _project(tmp, "pinned", stamp="1.150.0", mark="1.150.0", ignore=".aurora/scripts/aurora_common.py")
        busy = _project(tmp, "busy", stamp="1.150.0", mark="1.150.0")
        cur = _project(tmp, "cur", stamp="1.177.0", mark="1.177.0")
        assert ck.project_card(str(newer))["behind"] is False and ck.project_card(str(newer))["ahead"] is True
        assert ck.project_card(str(old))["behind"] is True
        assert "новее кита" in ck.version_gap(str(newer)), "проект новее кита (без замка движка) должен останавливать, как прежде"
        newer_locked = _project(tmp, "newer_locked", stamp="1.190.0", mark="1.190.0", lock={"version": "1.190.0", "engine_api": 1})
        assert ck.version_gap(str(newer_locked)) == "", "проект с замком движка, новее кита, остановил маршрут"
        assert ck.version_gap(str(cur)) == ""
        gap = ck.version_gap(str(old))
        assert "движок проекта 1.170.0" in gap, gap
        ck.KIT = str(kit_dir)
        locked = _project(tmp, "locked", stamp="1.170.0", mark="1.170.0", lock={"version": "1.170.0", "engine_api": 1})
        assert "ENGINE_API 1 → 2" in ck.version_gap(str(locked)), "рост контракта не назван в причине"
        calls = []
        ck.find_projects = lambda roots: [ck.project_card(str(p)) for p in (newer, old, pinned, busy, cur)]
        ck.kit_is_source = lambda: True

        real_run = saved[3]

        def fake_run(args, **kw):
            if len(args) > 2 and str(args[1]).endswith("aurora_update.py"):
                calls.append(args[2])
                return types.SimpleNamespace(returncode=0, stdout="✅ ok\n", stderr="")
            return real_run(args, **kw)               # прочие вызовы (карточки проектов спрашивают у системы) — настоящие
        ck.subprocess.run = fake_run
        with ck.JOBS_LOCK:
            ck.JOBS["job-busy"] = {"cmd": "kb:fix", "project": str(busy), "done": False}
        try:
            res = ck.update_all_projects([str(tmp)], True)
        finally:
            with ck.JOBS_LOCK:
                ck.JOBS.pop("job-busy", None)
        assert sorted(calls) == sorted([str(old)]), calls
        assert res["updated"] == 1 and [p["path"] for p in res["projects"]] == [str(old)], res
        got = {r["path"]: r["status"] for r in res["skipped"]}
        assert got == {str(newer): "ahead", str(pinned): "pinned", str(busy): "busy"}, res["skipped"]
    finally:
        ck.KIT, ck.find_projects, ck.kit_is_source, ck.subprocess.run = saved


@test
def test_update_does_not_downgrade_a_project_newer_than_the_kit(tmp: Path):
    """`update --apply` проекта, чей движок или штамп новее кита, не пишет ничего (код 4) и подсказывает обновить кит;
    флаг `--allow-downgrade` разрешает осознанный откат; просмотр без `--apply` только предупреждает."""
    _AC, AU, _ck = _mods()
    restore = set_home(tmp / "home")
    try:
        root = tmp / "proj"
        (root / "AuroraKnowledgeDB/meta").mkdir(parents=True)
        (root / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text("9.9.9\n", encoding="utf-8")
        (root / "aurora.config.yaml").write_text('project:\n  name: "PRJ-A"\n  slug: "prj-a"\n', encoding="utf-8")
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

        def cli(*args):
            return subprocess.run([sys.executable, str(SCRIPTS / "aurora_update.py"), str(root), *args], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=300)
        dry = cli()
        assert dry.returncode == 0 and "откажет без `--allow-downgrade`" in dry.stdout, dry.stdout[-500:]
        cp = cli("--apply")
        assert cp.returncode == 4 and "новее кита" in cp.stderr and "--allow-downgrade" in cp.stderr, (cp.returncode, cp.stderr[-500:])
        after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        assert after == before, "обновление написало файлы, отказав"
        cp = cli("--apply", "--allow-downgrade")
        assert cp.returncode == 0 and "Понижение разрешено" in cp.stdout, (cp.returncode, cp.stdout[-400:], cp.stderr[-400:])
        stamp = (root / "AuroraKnowledgeDB/meta/aurora_version.txt").read_text(encoding="utf-8").strip()
        assert stamp == AU.kit_version(), stamp
    finally:
        restore()
