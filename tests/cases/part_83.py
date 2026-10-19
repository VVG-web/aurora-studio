"""Проверки движка Aurora, часть 83: транзакционное обновление движка проекта (BL-LCY-07).

Каркас и помощники — tests/harness.py. Модуль `engine_txn` проверяет новые скрипты движка до первой записи
(компиляция, верхнеуровневые импорты локальных модулей и их имена), снимает копию заменяемых файлов вне проекта,
ведёт метку оборванного обновления, пишет `engine.lock.json` с хешами и сверяет записанное; `aurora_update` собирает
это в одну операцию с замком проекта, автоматическим откатом и командой `--rollback`. Домашняя папка подменена
на временную (снимки лежат в `~/.aurora/engine-backups/`), сеть и живые проекты проверкам не нужны.
"""
from __future__ import annotations

from pathlib import Path
import contextlib
import importlib
import io
import json
import os
import sys
import threading

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401


def _txn():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("engine_txn")


def _scripts(tmp: Path, files: dict) -> Path:
    d = tmp / "cur" / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return d


@test
def test_engine_check_passes_a_consistent_set_and_names_every_defect(tmp: Path):
    """Состав после обновления проверяется до записи: исправный проходит; синтаксис, отсутствующий модуль и имя, которого
    новый модуль не определяет (консолидация модулей), — отдельные проблемы; защищённые и вложенные импорты не ловятся."""
    T = _txn()
    cur = _scripts(tmp, {"aurora_common.py": "def old_name():\n    return 1\n", "other.py": "X = 1\n"})
    ok = {"scripts/aurora_common.py": "def helper():\n    return 2\n\nclass Card:\n    pass\n\nLIMIT = 3\n",
          "scripts/tool.py": "from aurora_common import helper, Card, LIMIT\nimport other\n\ndef main():\n    return helper()\n"}
    assert T.verify_overlay(ok, cur, {"aurora_common", "other", "tool"}) == []
    broken = {"scripts/aurora_common.py": "def helper(:\n", "scripts/tool.py": "import aurora_common\n"}
    assert any("не компилируется" in p and "aurora_common.py" in p for p in T.verify_overlay(broken, cur, {"aurora_common"}))
    missing_name = {"scripts/aurora_common.py": "def helper():\n    return 2\n",
                    "scripts/tool.py": "from aurora_common import helper, old_name\n"}
    got = T.verify_overlay(missing_name, cur, {"aurora_common"})
    assert len(got) == 1 and "old_name" in got[0] and "tool.py" in got[0], got
    gone = {"scripts/tool.py": "import vanished\n"}
    got = T.verify_overlay(gone, cur, {"vanished", "aurora_common"})
    assert len(got) == 1 and "vanished" in got[0], got
    soft = {"scripts/tool.py": ("try:\n    import vanished\nexcept ImportError:\n    vanished = None\n\n"
                                "def late():\n    from aurora_common import nothing_here\n    import vanished2\n")}
    assert T.verify_overlay(soft, cur, {"vanished", "vanished2", "aurora_common"}) == [], "защищённый и отложенный импорт приняты за дефект"
    star = {"scripts/aurora_common.py": "from other import *\n", "scripts/tool.py": "from aurora_common import whatever\n"}
    assert T.verify_overlay(star, cur, {"aurora_common", "other"}) == [], "модуль со звёздочкой судится по именам"
    assert T.verify_overlay({"docs/readme.md": "# нет"}, cur) == [], "не скрипты не проверяются"
    alone = {"scripts/tool.py": "import other\n"}
    assert T.verify_overlay(alone, tmp / "нет-такой-папки", {"other"}) != [], "модуль, которого нет вовсе, пропущен"


@test
def test_engine_check_passes_the_real_kit_scripts(tmp: Path):
    """Скрипты самого кита, как их положит обновление, проверку проходят: без ложных срабатываний на живом составе."""
    T = _txn()
    staged = {f"scripts/{p.name}": p.read_text(encoding="utf-8") for p in sorted(SCRIPTS.glob("*.py"))}
    problems = T.verify_overlay(staged, tmp / "empty", {p.stem for p in SCRIPTS.glob("*.py")})
    assert problems == [], problems[:5]


@test
def test_engine_snapshot_restores_files_and_deletes_the_ones_the_update_created(tmp: Path):
    """Снимок: заменяемый файл возвращается байт в байт, созданный обновлением — удаляется; хранятся три последних
    снимка проекта; повреждённая копия в снимке не подсовывается вместо файла."""
    T = _txn()
    restore_home = set_home(tmp / "home")
    try:
        proj = tmp / "proj"
        (proj / ".aurora/scripts").mkdir(parents=True)
        (proj / ".aurora/scripts/a.py").write_bytes("def a():\n    return 'старое'\n".encode("utf-8"))
        rels = [".aurora/scripts/a.py", ".aurora/scripts/new.py"]
        snap = T.snapshot(proj, rels, {"from": "1.100.0 (нет штампа)", "to": "1.101.0"})
        assert str(snap).startswith(str(tmp / "home" / ".aurora" / "engine-backups")), snap
        (proj / ".aurora/scripts/a.py").write_text("def a():\n    return 'новое'\n", encoding="utf-8")
        (proj / ".aurora/scripts/new.py").write_text("X = 1\n", encoding="utf-8")
        assert T.restore(proj, snap) == []
        assert (proj / ".aurora/scripts/a.py").read_bytes() == "def a():\n    return 'старое'\n".encode("utf-8")
        assert not (proj / ".aurora/scripts/new.py").exists(), "файл, созданный обновлением, остался"
        man = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
        assert man["from"].startswith("1.100.0") and man["files"][".aurora/scripts/new.py"]["existed"] is False
        copy = snap / "files" / ".aurora/scripts/a.py"
        copy.write_text("подмена", encoding="utf-8")
        (proj / ".aurora/scripts/a.py").write_text("пишет обновление", encoding="utf-8")
        failed = T.restore(proj, snap)
        assert failed and "хеш" in failed[0], failed
        assert (proj / ".aurora/scripts/a.py").read_text(encoding="utf-8") == "пишет обновление", "повреждённая копия записана"
        for n in range(4):
            T.snapshot(proj, rels, {"from": f"1.100.{n}", "to": "1.101.0"})
        shots = T.list_snapshots(proj)
        assert len(shots) == T.BACKUP_KEEP == 3, [str(s) for s, _ in shots]
        assert shots[0][1]["from"] == "1.100.3", "новые снимки идут первыми"
    finally:
        restore_home()


@test
def test_engine_pending_mark_lock_file_and_installed_check(tmp: Path):
    """Метка оборванного обновления видна, пока её не сняли, и не видна у живого (замок занят); `engine.lock.json`
    несёт хеши, версию, коммит кита и `ENGINE_API`; сверка после записи ловит подмену и мусор в скрипте."""
    T = _txn()
    proj = tmp / "proj"
    (proj / ".aurora/scripts").mkdir(parents=True)
    assert T.pending(proj) == {}
    T.mark_pending(proj, {"from": "1.1.0", "to": "1.2.0"})
    assert T.pending(proj)["to"] == "1.2.0"
    lock = T.update_lock(proj)
    assert lock.acquire(blocking=False)
    try:
        assert T.pending(proj) == {}, "обновление идёт, а его назвали оборванным"
        res = []
        t = threading.Thread(target=lambda: res.append(T.update_lock(proj).acquire(blocking=False)))
        t.start()
        t.join(30)
        assert res == [False], "два обновления проекта разом"
    finally:
        lock.release()
    assert T.pending(proj)["from"] == "1.1.0", "оборванное обновление не видно после смерти замка"
    T.clear_pending(proj)
    assert T.pending(proj) == {}
    good = "def run():\n    return 1\n"
    (proj / ".aurora/scripts/x.py").write_bytes(good.encode("utf-8"))
    (proj / ".aurora/commands.txt").write_text("a | b\n", encoding="utf-8")
    rec = T.write_lock(proj, KIT, "9.9.9", [".aurora/scripts/x.py", ".aurora/commands.txt", ".aurora/нет-такого.py"])
    on_disk = T.read_lock(proj)
    assert on_disk == rec and rec["version"] == "9.9.9" and rec["schema"] == 1, rec
    assert set(rec["files"]) == {".aurora/scripts/x.py", ".aurora/commands.txt"}, "в замок попал несуществующий файл"
    assert rec["files"][".aurora/scripts/x.py"] == T.sha256_text(good)
    assert isinstance(rec["engine_api"], int) and rec["engine_api"] >= 1
    assert T.check_installed(proj, {".aurora/scripts/x.py": T.sha256_text(good)}) == []
    (proj / ".aurora/scripts/x.py").write_bytes(b"def run(:\n")
    bad = T.check_installed(proj, {".aurora/scripts/x.py": T.sha256_text("def run(:\n")})
    assert bad and "не компилируется" in bad[0], bad
    bad = T.check_installed(proj, {".aurora/scripts/x.py": T.sha256_text(good), ".aurora/scripts/y.py": T.sha256_text("")})
    assert len(bad) == 2 and all("хеш" in b for b in bad), bad
    assert os.path.isfile(str(proj / ".aurora/engine.lock.json"))


# ------------------------------------------------------------------ полное обновление как одна операция

def _aged(tmp: Path, stamp: str = "1.0.0") -> Path:
    root = tmp / "proj"
    (root / "AuroraKnowledgeDB" / "meta").mkdir(parents=True)
    (root / "AuroraKnowledgeDB" / "meta" / "aurora_version.txt").write_text(stamp + "\n", encoding="utf-8")
    (root / "aurora.config.yaml").write_text('project:\n  name: "PRJ-A"\n  slug: "prj-a"\n', encoding="utf-8")
    (root / "AGENTS.md").write_text("# Свои правила проекта\n", encoding="utf-8")
    return root


def _au():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("aurora_update")


def _apply(AU, root: Path, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = AU.run(root, True, **kw)
    return rc, out.getvalue(), err.getvalue()


@test
def test_full_update_lays_the_engine_as_one_operation_and_seals_it(tmp: Path):
    """Полное обновление: снимок до записи, `engine.lock.json` с хешами файлов, версией кита и `ENGINE_API`, метка
    `update.pending` снята; повторное обновление ничего не переписывает."""
    AU, T = _au(), _txn()
    restore = set_home(tmp / "home")
    try:
        root = _aged(tmp)
        rc, out, err = _apply(AU, root)
        assert rc == 0, out[-600:] + err[-600:]
        lock = T.read_lock(root)
        assert lock["version"] == AU.kit_version() and lock["engine_api"] >= 1 and lock["schema"] == 1, lock
        for rel, sha in lock["files"].items():
            assert T.sha256_file(root / rel, normalize=True) == sha, rel
        assert ".aurora/scripts/aurora_fs.py" in lock["files"] and ".aurora/scripts/engine_txn.py" in lock["files"]
        assert T.pending(root) == {} and not (root / ".aurora/state/update.pending").exists()
        shots = T.list_snapshots(root)
        assert len(shots) == 1 and shots[0][1]["from"].startswith("1.0.0") and shots[0][1]["to"] == AU.kit_version(), shots
        first = (root / ".aurora/engine.lock.json").stat().st_mtime_ns
        rc, out, err = _apply(AU, root)
        assert rc == 0 and "уже актуальны" in out, out[-400:]
        assert (root / ".aurora/engine.lock.json").stat().st_mtime_ns == first, "повторное обновление переписало замок движка"
        assert len(T.list_snapshots(root)) == 1, "обновление без изменений сняло лишний снимок"
    finally:
        restore()


@test
def test_update_refuses_while_an_agent_runs_or_another_update_holds_the_project(tmp: Path):
    """Идущий прогон агента или другое обновление: `--apply` отказывает (код 3) и не пишет ни файла движка — один прогон
    двумя версиями хуже, чем отложенное обновление."""
    AU, T = _au(), _txn()
    restore = set_home(tmp / "home")
    try:
        root = _aged(tmp)
        assert _apply(AU, root)[0] == 0
        victim = root / ".aurora/scripts/kb_lint.py"
        good = victim.read_text(encoding="utf-8")
        victim.write_text("# устарел\n", encoding="utf-8")
        (root / ".aurora/state").mkdir(parents=True, exist_ok=True)
        (root / ".aurora/state/agent.lock").write_text(json.dumps({"pid": os.getpid(), "task": "distill"}), encoding="utf-8")
        rc, out, err = _apply(AU, root)
        assert rc == 3 and "идёт прогон агента" in err, (rc, err[-400:])
        assert victim.read_text(encoding="utf-8") == "# устарел\n", "движок переписан под идущим агентом"
        (root / ".aurora/state/agent.lock").unlink()
        held, release, res = threading.Event(), threading.Event(), []

        def hold():
            lock = T.update_lock(root)
            res.append(lock.acquire(blocking=False))
            held.set()
            release.wait(60)
            lock.release()
        t = threading.Thread(target=hold)
        t.start()
        assert held.wait(60) and res == [True]
        try:
            rc, out, err = _apply(AU, root)
            assert rc == 3 and "уже идёт" in err, (rc, err[-400:])
            assert victim.read_text(encoding="utf-8") == "# устарел\n"
        finally:
            release.set()
            t.join(60)
        assert _apply(AU, root)[0] == 0 and victim.read_text(encoding="utf-8") == good, "после отказов обновление не прошло"
    finally:
        restore()


@test
def test_update_refuses_a_broken_engine_set_before_the_first_write(tmp: Path):
    """Набор, где скрипт не компилируется или импортирует несуществующее имя, не ложится в проект вовсе: код 5, прежние
    файлы целы, метки обновления нет."""
    AU, T = _au(), _txn()
    restore = set_home(tmp / "home")
    real_plan = AU.plan
    try:
        root = _aged(tmp)
        assert _apply(AU, root)[0] == 0
        keep = (root / ".aurora/scripts/aurora_common.py").read_text(encoding="utf-8")
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / ".aurora").rglob("*") if p.is_file()}
        for label, files in (("синтаксис", {".aurora/scripts/new_tool.py": "def broken(:\n"}),
                             ("имя", {".aurora/scripts/aurora_common.py": "def only_one():\n    return 1\n",
                                      ".aurora/scripts/new_tool.py": "from aurora_common import utc_today\n"})):
            AU.plan = lambda target, f, _files=files: [AU.Change("write", rel, text, "проверка") for rel, text in _files.items()]
            rc, out, err = _apply(AU, root)
            assert rc == 5 and "не обновлён" in err and "проверка до записи" in err, (label, rc, err[-500:])
            after = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / ".aurora").rglob("*") if p.is_file()}
            assert after == before, f"{label}: движок тронут до проверки"
            assert (root / ".aurora/scripts/aurora_common.py").read_text(encoding="utf-8") == keep
            assert T.pending(root) == {}
    finally:
        AU.plan = real_plan
        restore()


@test
def test_update_write_failure_returns_the_engine_from_the_snapshot(tmp: Path):
    """Сбой записи на середине набора (диск полон, файл занят): файлы возвращаются из снимка, созданные удаляются,
    код 6, метка снята; повторное обновление после устранения сбоя проходит."""
    AU, T = _au(), _txn()
    restore = set_home(tmp / "home")
    real_write = AU._write_engine_file
    try:
        root = _aged(tmp)
        assert _apply(AU, root)[0] == 0
        old = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / ".aurora").rglob("*") if p.is_file()}
        for rel in (".aurora/scripts/kb_lint.py", ".aurora/scripts/kb_fix.py", ".aurora/scripts/kb_moc.py"):
            (root / rel).write_text("# устарел\n", encoding="utf-8")
        (root / ".aurora/scripts/stray_new.py").write_text("X = 1\n", encoding="utf-8")
        stale = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / ".aurora").rglob("*") if p.is_file()}
        seen = []

        def flaky(dst, text):
            seen.append(dst.name)
            if len(seen) == 3:
                raise OSError(28, "диск полон (проверка)")
            return real_write(dst, text)
        AU._write_engine_file = flaky
        rc, out, err = _apply(AU, root)
        assert rc == 6 and "файлы возвращены из снимка" in err and "диск полон" in err, (rc, err[-500:])
        now = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / ".aurora").rglob("*") if p.is_file()}
        assert now == stale, sorted(k for k in set(now) | set(stale) if now.get(k) != stale.get(k))[:6]
        assert T.pending(root) == {}
        AU._write_engine_file = real_write
        assert _apply(AU, root)[0] == 0
        assert (root / ".aurora/scripts/kb_lint.py").read_bytes() == old[".aurora/scripts/kb_lint.py"]
    finally:
        AU._write_engine_file = real_write
        restore()


@test
def test_interrupted_update_is_visible_and_rollback_returns_the_previous_engine(tmp: Path):
    """Обрыв посреди записи (`KeyboardInterrupt`): метка `update.pending` остаётся, статус — `partial`, гейт маршрута
    останавливает; повторное обновление достраивает и снимает метку; `--rollback` возвращает движок и штамп из снимка."""
    AU, T = _au(), _txn()
    import aurora_common as AC
    restore = set_home(tmp / "home")
    real_write = AU._write_engine_file
    try:
        root = _aged(tmp)
        assert _apply(AU, root)[0] == 0
        kit = AU.kit_version()
        (root / ".aurora/engine_version.txt").write_text("1.100.0\n", encoding="utf-8")
        (root / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text("1.100.0\n", encoding="utf-8")
        for name in ("kb_lint", "kb_fix", "kb_moc", "kb_index", "kb_names", "kb_gaps"):
            (root / ".aurora/scripts" / (name + ".py")).write_text("# версия 1.100.0\n", encoding="utf-8")
        (root / ".aurora/engine.lock.json").unlink()
        before = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / ".aurora").rglob("*")
                  if p.is_file() and "state" not in p.relative_to(root).parts}
        calls = []

        def die(dst, text):
            calls.append(dst.name)
            if len(calls) == 5:
                raise KeyboardInterrupt
            return real_write(dst, text)
        AU._write_engine_file = die
        try:
            _apply(AU, root)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("обрыв не сработал")
        AU._write_engine_file = real_write
        assert T.pending(root)["to"] == kit, "оборванное обновление не оставило метки"
        st = AC.engine_status(root, kit, AC.kit_engine_api(KIT))
        assert st["status"] == "partial" and st["blocks"], st
        rc, out, err = _apply(AU, root)
        assert rc == 0 and "оборвалось на середине" in out, out[-500:]
        assert T.pending(root) == {} and AC.engine_status(root, kit, 1)["status"] == "ok"
        # откат после «обрыв → достраивание»: прежний снимок дополняется, а не снимается заново, поэтому откат ведёт к
        # состоянию ДО первой попытки (а не к полусобранному)
        assert len(T.list_snapshots(root)) == 2, "достраивание сняло новый снимок вместо дополнения прежнего"
        dry = io.StringIO()
        with contextlib.redirect_stdout(dry):
            assert AU.run_rollback(root, False) == 0
        assert "(dry-run)" in dry.getvalue() and "вернётся файлов движка" in dry.getvalue(), dry.getvalue()
        with contextlib.redirect_stdout(io.StringIO()):
            assert AU.run_rollback(root, True) == 0
        assert (root / "AuroraKnowledgeDB/meta/aurora_version.txt").read_text(encoding="utf-8").strip() == "1.100.0", \
            "штамп проекта не вернулся"
        assert T.pending(root) == {}
        after = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / ".aurora").rglob("*")
                 if p.is_file() and "state" not in p.relative_to(root).parts}
        mixed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        assert not mixed, f"откат оставил смешанный движок: {mixed[:6]}"
    finally:
        AU._write_engine_file = real_write
        restore()


@test
def test_rollback_without_snapshots_and_on_a_busy_project_says_so(tmp: Path):
    """`--rollback`: снимков нет — понятная причина, код 1; идёт агент — отказ с кодом 3; просмотр без `--apply` не пишет."""
    AU = _au()
    restore = set_home(tmp / "home")
    try:
        root = _aged(tmp)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            assert AU.run_rollback(root, True) == 1
        assert "Снимков движка этого проекта нет" in err.getvalue(), err.getvalue()
        assert _apply(AU, root)[0] == 0
        (root / ".aurora/state").mkdir(parents=True, exist_ok=True)
        (root / ".aurora/state/agent.lock").write_text(json.dumps({"pid": os.getpid(), "task": "build"}), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            assert AU.run_rollback(root, True) == 3
        assert "идёт прогон агента" in err.getvalue()
    finally:
        restore()


@test
def test_engine_prepare_clears_its_mark_when_part_of_the_engine_is_shared_or_newer(tmp: Path):
    """Подготовка движка с общей папкой (ссылка) или с копией новее кита кладёт файлы, не объявляет замок и метку версии,
    но метку обновления снимает: иначе проект навсегда числился бы `partial` и маршруты стояли."""
    AU, T = _au(), _txn()
    import aurora_common as AC
    restore = set_home(tmp / "home")
    try:
        root = _aged(tmp, stamp="1.0.0")
        shared = tmp / "shared-docs"
        shared.mkdir()
        (shared / "knowledge-rules.md").write_text("# общий файл\n", encoding="utf-8")
        (root / ".aurora").mkdir()
        try:
            os.symlink(shared, root / ".aurora/docs", target_is_directory=True)
        except (OSError, NotImplementedError):
            return                                       # ссылок нельзя (Windows без прав)
        res = AU.engine_prepare(root)
        assert res["ok"] and res["shared"], res
        assert T.read_pending(root) == {}, "метка обновления осталась после успешной подготовки"
        assert AC.engine_status(root, AU.kit_version(), 1)["status"] != "partial"
        # копия новее кита и не хватает файла
        (root / ".aurora/engine_version.txt").write_text("9.9.9\n", encoding="utf-8")
        (root / ".aurora/scripts/kb_lint.py").unlink()
        res = AU.engine_prepare(root)
        assert res["ahead"] and res["ok"] and T.read_pending(root) == {}, res
        assert AC.engine_status(root, AU.kit_version(), 1)["status"] == "ahead"
    finally:
        restore()


@test
def test_update_does_not_start_when_the_snapshot_cannot_be_taken(tmp: Path):
    """Домашняя папка недоступна для записи (контейнер, служебная учётная запись): обновление не начинается и ничего не
    пишет в движок (код 5), а не падает трассировкой посреди работы; предпросмотр набора, который не пройдёт проверку,
    предупреждает заранее."""
    AU, T = _au(), _txn()
    restore = set_home(tmp / "home")
    real_root, real_plan = T.backups_root, AU.plan
    try:
        root = _aged(tmp)
        blocker = tmp / "not-a-folder"
        blocker.write_text("файл на месте каталога", encoding="utf-8")
        T.backups_root = lambda: blocker / "engine-backups"
        rc, out, err = _apply(AU, root)
        assert rc == 5 and "снимок" in err and "не начато" in err and "Traceback" not in err, (rc, err[-500:])
        assert not (root / ".aurora/scripts").exists(), "файлы движка записаны без снимка"
        T.backups_root = real_root
        AU.plan = lambda target, f: [AU.Change("write", ".aurora/scripts/new_tool.py", "def broken(:\n", "проверка")]
        out_io = io.StringIO()
        with contextlib.redirect_stdout(out_io):
            assert AU.run(root, False) == 0
        assert "не пройдёт проверку" in out_io.getvalue() and "(dry-run)" in out_io.getvalue(), out_io.getvalue()[-500:]
    finally:
        T.backups_root, AU.plan = real_root, real_plan
        restore()


@test
def test_rollback_never_trusts_paths_or_links_from_outside_the_engine(tmp: Path):
    """Манифест снимка лежит вне проекта: путь вне `.aurora/` (с `..`, абсолютный, через ссылку), чужой проект и испорченные
    поля откат отвергает, не тронув файлы; ссылка в проекте не затирается записью файла замка или метки; снимки и копии в
    них приватны (0700/0600)."""
    T = _txn()
    F = importlib.import_module("aurora_fs")
    restore_home = set_home(tmp / "home")
    try:
        proj = tmp / "proj"
        (proj / ".aurora/scripts").mkdir(parents=True)
        (proj / ".aurora/scripts/a.py").write_text("X = 1\n", encoding="utf-8")
        outside = tmp / "outside.txt"
        outside.write_text("чужой файл", encoding="utf-8")
        snap = T.snapshot(proj, [".aurora/scripts/a.py"], {"from": "1.0.0", "to": "1.1.0"})
        man = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
        evil = {**man, "files": {**man["files"], "../outside.txt": {"existed": False, "sha256": ""},
                                 str(outside): {"existed": False, "sha256": ""},
                                 ".aurora/../../outside.txt": {"existed": False, "sha256": ""}, "list": 5}}
        (snap / "manifest.json").write_text(json.dumps(evil), encoding="utf-8")
        (proj / ".aurora/scripts/a.py").write_text("X = 2\n", encoding="utf-8")
        failed = T.restore(proj, snap)
        assert outside.read_text(encoding="utf-8") == "чужой файл", "откат тронул файл вне проекта"
        assert len(failed) == 4 and all("пропущен" in f for f in failed), failed
        assert (proj / ".aurora/scripts/a.py").read_text(encoding="utf-8") == "X = 1\n", "годный файл не вернулся"
        other = tmp / "other"
        (other / ".aurora").mkdir(parents=True)
        assert "другого проекта" in T.restore(other, snap)[0]
        (snap / "manifest.json").write_text(json.dumps({**man, "files": ["не словарь"]}), encoding="utf-8")
        assert "нет списка файлов" in T.restore(proj, snap)[0]
        # ссылка на месте метки и замка: цель не затирается
        victim = tmp / "victim.txt"
        victim.write_text("мои данные", encoding="utf-8")
        (proj / ".aurora/state").mkdir()
        try:
            os.symlink(victim, proj / ".aurora/state/update.pending")
            os.symlink(victim, proj / ".aurora/state/update.lock")
        except (OSError, NotImplementedError):
            return
        T.mark_pending(proj, {"from": "1", "to": "2"})
        assert victim.read_text(encoding="utf-8") == "мои данные", "метка записана сквозь ссылку"
        lock = T.update_lock(proj)
        try:
            lock.acquire(timeout=0.2)
        except OSError:
            pass
        else:
            raise AssertionError("замок взят на файле-ссылке")
        assert victim.read_text(encoding="utf-8") == "мои данные", "замок усёк файл по ссылке"
        if os.name != "nt":
            base = T.backups_root()
            assert (base.stat().st_mode & 0o077) == 0 and (snap.stat().st_mode & 0o077) == 0
            assert (snap / "manifest.json").stat().st_mode & 0o077 == 0
            assert (snap / "files/.aurora/scripts/a.py").stat().st_mode & 0o077 == 0
        assert F.is_locked(proj / ".aurora/state/update.lock") is False
    finally:
        restore_home()


@test
def test_read_state_survives_pipes_and_absurd_nesting(tmp: Path):
    """Состояние от третьих рук: канал FIFO на месте файла не вешает чтение, глубокая вложенность и огромный размер не
    роняют его исключением — значение по умолчанию."""
    F = importlib.import_module("aurora_fs")
    deep = tmp / "deep.json"
    deep.write_text("[" * 200000, encoding="utf-8")
    assert F.read_state(deep, [], quarantine=False) == []
    big = tmp / "big.json"
    big.write_text("[1]", encoding="utf-8")
    real = F.MAX_STATE_BYTES
    F.MAX_STATE_BYTES = 1
    try:
        assert F.read_state(big, [9], quarantine=False) == [9]
    finally:
        F.MAX_STATE_BYTES = real
    if hasattr(os, "mkfifo"):
        fifo = tmp / "pipe.json"
        os.mkfifo(fifo)
        t0 = __import__("time").monotonic()
        assert F.read_state(fifo, {"d": 1}, quarantine=False) == {"d": 1}
        assert __import__("time").monotonic() - t0 < 5, "чтение канала повисло"


@test
def test_rollback_needs_the_snapshot_not_the_kit(tmp: Path):
    """`--rollback` не требует кита рядом: откату нужны снимки в `~/.aurora`, поэтому он отвечает про снимки, а не
    «запустите из самого кита»."""
    AU = _au()
    restore = set_home(tmp / "home")
    real_manifest, real_argv = AU.MANIFEST, sys.argv
    try:
        root = _aged(tmp)
        AU.MANIFEST = tmp / "no-such-manifest.txt"
        sys.argv = ["aurora_update.py", str(root), "--rollback"]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = AU.main()
        assert rc == 1 and "Снимков движка" in err.getvalue(), (rc, err.getvalue()[-300:])
    finally:
        AU.MANIFEST, sys.argv = real_manifest, real_argv
        restore()
