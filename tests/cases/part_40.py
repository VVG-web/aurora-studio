"""Проверки движка Aurora, часть 40. Каркас и помощники — tests/harness.py.

Приёмочный набор жизненного цикла (BL-LCY-02): обновление проекта, поставленного прежним
движком (N→N+k), обрыв `update` посреди записи, два `update` одновременно, повторный `update`.
Раньше тестовый проект всегда нёс текущий движок (`make_project` копирует `scripts/*.py`), и
ни один сценарий «проект старше кита» не доезжал до проверки целиком: только точечные
случаи (переезд из `.opencode`, выведенный скрипт, хук).

Проекты здесь заводятся настоящей командой `aurora.py new` — без подмен. Прежние версии
берутся из git-тегов (`git archive vX.Y.Z` во временную папку, копий в репозитории нет);
на машине без тегов (мелкий клон) эти сценарии молча пропускаются, а в проверке GitHub,
где `AURORA_REQUIRE_TAGS=1`, отсутствие тегов — падение. Домашняя папка прогона временная
(`harness._own_home_for_the_run`), и каждый запуск, который пишет, это перепроверяет.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from harness import (  # noqa: F401
    KIT,
    SCRIPTS,
    card,
    test,
)

# Прежние выпуски: до переезда движка из `.opencode/` (1.158.0), сразу перед ним и после.
HISTORY_TAGS = ("v1.140.0", "v1.157.1", "v1.169.0")


def _own_home() -> None:
    """Запуск, который пишет, идёт только под временным HOME: настоящий `~` владельца неприкосновенен."""
    real = os.environ.get("AURORA_TESTS_REAL_HOME")
    assert os.environ.get("AURORA_TESTS_HOME"), "прогон без временной домашней папки"
    assert Path.home().resolve() != Path(real).resolve() if real else True, \
        f"HOME указывает на настоящую домашнюю папку: {Path.home()}"


# Прежний выпуск кита печатал русские строки в трубу как есть: на Windows (cp1252 в трубе) `aurora.py new`
# выпуска 1.140.0 падал на первой же строке. Выпуск менять нельзя, а проверяется обновление, а не его консоль:
# прежний кит запускается в UTF-8 — так его и запускает панель (`child_env`). Текущий кит — как есть.
OLD_KIT_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _py(script: Path, *args, cwd: Path, expect_rc=0, env=None) -> subprocess.CompletedProcess:
    _own_home()
    cp = subprocess.run([sys.executable, str(script), *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        env={**os.environ, **env} if env else None)
    if expect_rc is not None:
        assert cp.returncode == expect_rc, \
            f"{script.name} {' '.join(args)}: rc={cp.returncode}, ждали {expect_rc}\n" \
            f"{cp.stdout[-900:]}\n{cp.stderr[-600:]}"
    return cp


def _tags() -> set:
    cp = subprocess.run(["git", "tag", "--list"], cwd=str(KIT), capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    return set(cp.stdout.split()) if cp.returncode == 0 else set()


def _tagged_kit(tag: str, dest: Path) -> Path:
    """Кит выпуска `tag`: `git archive` в `dest`. Из репозитория ничего не копируется и не хранится."""
    cp = subprocess.run(["git", "archive", "--format=tar", tag], cwd=str(KIT), capture_output=True)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(cp.stdout)) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)
    return dest


def _new_project(kit: Path, root: Path, name: str = "Old", slug: str = "") -> Path:
    """Проект, заведённый китом `kit` настоящей командой `new` (без терминала)."""
    _py(kit / "aurora.py", "new", str(root), "--name", name, "--slug", slug or name, cwd=root.parent,
        env=None if kit == KIT else OLD_KIT_ENV)
    return root


def _tree(root: Path) -> dict:
    """Содержимое проекта: путь → хеш. Кэш байт-кода и сам git — не содержимое.

    Концы строк не в счёт: клон кита на Windows (`core.autocrlf`) отдаёт файлы с CRLF, установка копирует их
    байтами, а обновление пишет текст, прочитанный с переводом строк в `\\n` — разница только в `\\r`, и сравнивать
    «свежий» проект с «обновлённым» по ней значило бы проверять настройку git, а не движок."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if ".git/" in rel + "/" or "__pycache__" in rel or rel.endswith(".pyc") or not p.is_file():
            continue
        out[rel] = hashlib.sha1(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return out


def _tree_without_install_report(root: Path) -> dict:
    return {k: v for k, v in _tree(root).items()
            if not k.endswith("_report_aurora-install.md") and not k.endswith("/.gitkeep")}


def _git_commit_all(root: Path) -> None:
    # Без фонового обслуживания: `git commit` запускает `maintenance run --auto` отдельным
    # процессом, и он перебирает `.git/objects`, пока проверка копирует проект (ENOENT).
    for args in (["init", "-q"], ["config", "gc.auto", "0"], ["config", "maintenance.auto", "false"],
                 ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _git_status(root: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def _update(kit: Path, root: Path, *, apply: bool = True, expect_rc=0) -> subprocess.CompletedProcess:
    return _py(kit / "scripts" / "aurora_update.py", str(root), *(["--apply"] if apply else []),
               cwd=root, expect_rc=expect_rc, env=None if kit == KIT else OLD_KIT_ENV)


def _stamp(root: Path) -> str:
    f = root / "AuroraKnowledgeDB/meta/aurora_version.txt"
    return f.read_text(encoding="utf-8").strip() if f.is_file() else ""


def _plain_manifest_rows() -> list:
    """Строки манифеста без спецправил: файл проекта обязан совпасть с файлом кита 1:1."""
    rows = []
    for line in (KIT / "engine_manifest.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        src, dst = (x.strip() for x in line.split("=>", 1))
        if not re.match(r"\(\w+\)", dst):
            rows.append((src, dst))
    return rows


def _assert_engine_is_current(root: Path) -> None:
    """Каждый файл движка в проекте — тот, что в ките (манифест, а не «что скопировал фикстурщик»)."""
    rows = _plain_manifest_rows()
    assert len(rows) > 50, f"манифест прочитан неверно: {len(rows)} строк"
    for src, dst in rows:
        got = root / dst
        assert got.is_file(), f"после обновления нет {dst}"
        assert got.read_text(encoding="utf-8") == (KIT / src).read_text(encoding="utf-8"), \
            f"{dst} отличается от кита ({src})"
    assert _stamp(root) == (KIT / "VERSION").read_text(encoding="utf-8").strip(), \
        f"штамп версии проекта {_stamp(root)!r} ≠ кит"


def _put_user_data(root: Path) -> dict:
    """Данные человека, которых обновление касаться не вправе → их хеши."""
    # две карточки ссылаются друг на друга: одинокая карточка — ошибка линтера, а не данные
    card(root, "Concepts/Моя карточка.md", "Текст человека — с кириллицей и «ёлочками». См. [[Вторая карточка]].",
         status="draft")
    card(root, "Concepts/Вторая карточка.md", "Назад: [[Моя карточка]].", status="draft")
    (root / "Raw").mkdir(exist_ok=True)
    (root / "Raw/заметка.md").write_text("сырой материал\n", encoding="utf-8")
    prompts = root / "Prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "мой-промпт.md").write_text("проектный промпт\n", encoding="utf-8")
    mine = {rel: hashlib.sha1((root / rel).read_bytes()).hexdigest()
            for rel in ("AuroraKnowledgeDB/Concepts/Моя карточка.md",
                        "AuroraKnowledgeDB/Concepts/Вторая карточка.md", "Raw/заметка.md",
                        "Prompts/мой-промпт.md")}
    return mine


def _assert_user_data_intact(root: Path, mine: dict) -> None:
    for rel, h in mine.items():
        p = root / rel
        assert p.is_file(), f"обновление потеряло данные проекта: {rel}"
        assert hashlib.sha1(p.read_bytes()).hexdigest() == h, f"обновление изменило данные проекта: {rel}"
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert 'name: "Old"' in cfg or "name: Old" in cfg, f"конфиг потерял имя проекта:\n{cfg[:300]}"


@test
def test_lifecycle_update_from_past_releases_n_to_n_plus_k(tmp: Path):
    """Проект, заведённый выпуском N, обновляется текущим китом и остаётся рабочим.

    Для каждого прежнего выпуска: `aurora.py new` его же китом, данные человека, git, затем
    обновление текущим китом. Проверяется то, ради чего обновление и существует: движок в
    проекте — ровно файлы манифеста, штамп версии, данные человека на месте, `doctor` и
    `kb_lint` проходят, а второе обновление ничего не меняет ни на диске, ни в git. Ещё одна
    цепочка идёт через промежуточный выпуск (N→N+1→N+k) и обязана дойти до того же состояния.
    """
    have = _tags()
    wanted = [t for t in HISTORY_TAGS if t in have]
    if os.environ.get("AURORA_REQUIRE_TAGS") == "1":
        assert wanted == list(HISTORY_TAGS), \
            f"в проверке нет тегов {sorted(set(HISTORY_TAGS) - have)} (checkout без fetch-depth: 0?)"
    if not wanted:
        return                                   # мелкий клон без тегов: нечего брать «из прошлого»
    final = {}
    for tag in wanted:
        kit = _tagged_kit(tag, tmp / f"kit-{tag}")
        root = _new_project(kit, tmp / f"p-{tag}")
        assert _stamp(root) == tag.lstrip("v"), (tag, _stamp(root))
        mine = _put_user_data(root)
        _git_commit_all(root)

        dry = _update(KIT, root, apply=False)
        assert "Ничего не записано" in dry.stdout, dry.stdout[-400:]
        assert _stamp(root) == tag.lstrip("v"), "dry-run проставил штамп"

        _update(KIT, root)
        _assert_engine_is_current(root)
        _assert_user_data_intact(root, mine)
        assert not (root / ".opencode/scripts").exists(), f"{tag}: копии кита остались в .opencode/"
        _py(root / ".aurora/scripts/aurora_doctor.py", cwd=root, expect_rc=0)
        _py(root / ".aurora/scripts/kb_lint.py", "--summary", cwd=root, expect_rc=0)

        before, status = _tree(root), _git_status(root)
        again = _update(KIT, root)
        # шаблоны и промпты, правленые проектом, по замыслу каждый раз ложатся рядом как `.new`
        # (тем же содержимым) — а вот движку делать нечего
        assert "Инженерные файлы к перезаписи" not in again.stdout or \
            "Инженерные файлы к перезаписи: 0" in again.stdout, \
            f"{tag}: второе обновление нашло работу в движке:\n{again.stdout[-700:]}"
        assert _tree(root) == before, f"{tag}: второе обновление изменило проект"
        assert _git_status(root) == status, f"{tag}: второе обновление изменило git"
        final[tag] = root

    # N→N+1→N+k: сначала выпуск посередине, потом текущий кит — состояние то же, что при прямом.
    if "v1.140.0" in final and "v1.157.1" in have:
        mid = _tagged_kit("v1.157.1", tmp / "kit-mid")
        root = _new_project(_tagged_kit("v1.140.0", tmp / "kit-from"), tmp / "p-chain")
        _update(mid, root)
        assert _stamp(root) == "1.157.1", _stamp(root)
        _update(KIT, root)
        _assert_engine_is_current(root)
        engine = lambda r: {k: v for k, v in _tree(r).items() if k.startswith(".aurora/")}  # noqa: E731
        assert engine(root) == engine(final["v1.140.0"]), "цепочка через 1.157.1 дала иной движок, чем прямое обновление"


def _aged_project(tmp: Path, name: str = "Old", old_world: bool = False) -> Path:
    """Проект текущего кита, «состаренный» до старого состояния: чужая версия, правленые и пропавшие файлы.

    Работает без тегов, поэтому обновление «проект старше кита» проверяется на любой машине.

    `old_world=True` — проект таким, каким его держит большинство живых: кириллица в имени,
    значения доверия в конфиге пусты (их обновление дописывает), в git с хуком, у `.gitignore`
    и `.gitattributes` нет правил, появившихся позже. Именно такой проект заставляет `update`
    писать конфиг, `.gitignore`, `.gitattributes` и хук: у только что заведённого проекта
    эти записи не нужны, и обрыв на них проверять не на чем.
    """
    root = _new_project(KIT, tmp / "aged", name, "old" if old_world else "")
    (root / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text("1.100.0\n", encoding="utf-8")
    scripts = root / ".aurora/scripts"
    (scripts / "kb_lint.py").write_text("# старая копия\n", encoding="utf-8")
    (scripts / "aurora_doctor.py").unlink()
    (scripts / "kb_queue.py").write_text("# выведенный скрипт\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("старый AGENTS\n", encoding="utf-8")
    (root / ".aurora/structure_dirs.txt").unlink()
    # половина движка пропала или стоит старой копией: обновлению есть что записывать
    # Выведенный скрипт, установщик хука и общий модуль остаются как поставлены: кому из
    # скриптов выпадет чёт, зависит от числа и имён скриптов движка, и новый модуль по алфавиту
    # раньше них стирал именно их (тесты хука и выведенного скрипта падали от чужого нового файла).
    for i, f in enumerate(sorted(scripts.glob("*.py"))):
        if f.name in ("kb_queue.py", "aurora_hooks.py", "aurora_common.py"):
            continue
        if i % 2:
            f.unlink() if i % 4 == 1 else f.write_text("# старая копия\n", encoding="utf-8")
    for folder in ("docs", "skills", "connectors"):
        shutil.rmtree(root / ".aurora" / folder, ignore_errors=True)
    shutil.rmtree(root / "Deliverables/_archive", ignore_errors=True)
    if old_world:
        cfg = root / "aurora.config.yaml"
        text = re.sub(r"(?m)^(\s*(?:trust_statuses|trusted_sources)\s*:).*$", r"\1 []",
                      cfg.read_text(encoding="utf-8"))
        cfg.write_text(text, encoding="utf-8")
        (root / ".gitignore").write_text("# мой список\n.DS_Store\n.env\n", encoding="utf-8")
        (root / ".gitattributes").write_text("*.md text eol=lf\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
        _py(root / ".aurora/scripts/aurora_hooks.py", "--install", cwd=root)
        _git_commit_all(root)
    return root


@test
def test_lifecycle_update_of_aged_project_equals_fresh_install(tmp: Path):
    """Состаренный проект после обновления неотличим от свежего, а данные человека целы.

    Не требует тегов. «Свежий» — тем же `aurora.py new` с теми же именем и slug: если после
    обновления остаётся разница (забытый файл, выведенный скрипт, штамп), обновление
    приводит проект не к тому состоянию, в которое его ставит установка.
    """
    root = _aged_project(tmp)
    mine = _put_user_data(root)
    fresh = _new_project(KIT, tmp / "fresh")
    _put_user_data(fresh)

    _update(KIT, root)
    _assert_engine_is_current(root)
    _assert_user_data_intact(root, mine)
    assert not (root / ".aurora/scripts/kb_queue.py").exists(), "выведенный скрипт остался"
    # отчёт об установке — след самой установки, а не состояние движка
    a, b = (_tree_without_install_report(r) for r in (root, fresh))
    diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    assert not diff, f"обновлённый проект отличается от свежего: {diff[:8]}"

    before = _tree(root)
    again = _update(KIT, root)
    assert "уже актуальны" in again.stdout, again.stdout[-400:]
    assert _tree(root) == before, "повторное обновление изменило проект"
    _update(KIT, root, apply=False)
    assert _tree(root) == before, "dry-run изменил проект"


class _Fault:
    """Обрыв посреди обновления: n-я по счёту запись или удаление внутри проекта.

    `torn=False` — отказ до записи (диск полон, нет прав); `torn=True` — запись обрывается
    на половине файла (убитый процесс). Вместо обрыва можно вызвать `hook` — например,
    запустить второе обновление «в этот же момент». Счётчик считает только операции внутри
    `target`, остальное (кэш, HOME) не его дело.
    """

    def __init__(self, target: Path, at: int = 0, torn: bool = False, hook=None):
        # и настоящий путь тоже: запись через временный файл идёт по `realpath` (на macOS
        # `/var/…` временной папки — ссылка на `/private/var/…`)
        self.target, self.at, self.torn, self.hook = str(target), at, torn, hook
        self.roots = (str(target), os.path.realpath(target))
        self.count = 0
        self.fired = False     # дошёл ли счёт до n: мёртвая инъекция не должна давать зелёный тест
        self.ops = []          # путь каждой операции по порядку: чтобы выбрать, где обрывать
        self.busy = False
        self._saved = {}

    def _mine(self, p) -> bool:
        return str(p).startswith(self.roots)

    def _tick(self, path, partial) -> None:
        if self.busy or not self._mine(path):
            return
        self.count += 1
        self.ops.append(str(path))
        if self.count != self.at:
            return
        self.fired = True
        if self.hook:
            self.busy = True
            try:
                self.hook()
            finally:
                self.busy = False
            return
        if self.torn and partial is not None:
            partial()
        raise OSError(28, "обрыв посреди обновления (проверка)")

    def __enter__(self):
        P = Path
        self._saved = {n: getattr(P, n) for n in ("write_bytes", "write_text", "unlink")}
        fault = self

        def write_bytes(p, data):
            fault._tick(p, lambda: self._saved["write_bytes"](p, bytes(data)[:len(data) // 2]))
            return self._saved["write_bytes"](p, data)

        def write_text(p, data, *a, **kw):
            fault._tick(p, lambda: self._saved["write_text"](p, data[:len(data) // 2], *a, **kw))
            return self._saved["write_text"](p, data, *a, **kw)

        def unlink(p, *a, **kw):
            fault._tick(p, None)
            return self._saved["unlink"](p, *a, **kw)

        P.write_bytes, P.write_text, P.unlink = write_bytes, write_text, unlink
        return self

    def __exit__(self, *exc):
        for n, f in self._saved.items():
            setattr(Path, n, f)
        return False


def _au():
    """Обновление как модуль: чтобы подменять запись внутри процесса."""
    import importlib
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("aurora_update")


def _run_in_process(AU, root: Path, ops: list | None = None, fired: list | None = None, **fault):
    """Один `update --apply` в этом процессе; → (число операций записи, исключение или None).

    `ops`, если дан, пополняется путями операций по порядку, `fired` — признаком, что обрыв
    (или вторая сторона гонки) в этот прогон действительно случился.
    """
    f = _Fault(root, **fault)
    err = None
    with f, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            AU.run(root, True)
        except Exception as e:  # noqa: BLE001 — обрыв проверки и есть исключение
            err = e
    if ops is not None:
        ops.extend(f.ops)
    if fired is not None:
        fired.append(f.fired)
    return f.count, err


@test
def test_lifecycle_update_interrupted_midway_leaves_project_recoverable(tmp: Path):
    """Обрыв `update` посреди записи: проект не врёт про версию, повтор достраивает его до чистого состояния.

    Проект — «старого мира» (`_aged_project(old_world=True)`): конфиг с пустым доверием,
    `.gitignore`, `.gitattributes` и хук в git, кириллица в имени; у только что заведённого
    проекта этих записей нет, и обрыв на них проверять не на чем. Обрыв — отказ n-й записи и
    «убитый процесс» (запись обрывается на половине файла); n — каждая запись единственных
    файлов проекта (конфиг, `.gitignore`, `.gitattributes`, штамп, `kit_path.txt`) и выборка
    остальных. Штамп версии остаётся прежним (он ставится последним: проект не выдаёт себя за
    обновлённый), единственные файлы никогда не остаются обрезком, а повторное обновление
    приводит дерево ровно к тому, что дал бы непрерывный прогон.
    """
    AU = _au()
    base = _aged_project(tmp, "Старый проект", old_world=True)
    _put_user_data(base)
    ref = tmp / "ref"
    shutil.copytree(base, ref, symlinks=True)
    ops: list = []
    total, err = _run_in_process(AU, ref, ops=ops)
    assert err is None, f"непрерывное обновление упало: {str(repr(err))[:300]}"
    reference = _tree(ref)
    assert total > 40, f"подсчёт операций записи не сработал: {total}"

    # Файлы, которые у проекта в единственном экземпляре и которые человек правит сам: обрезок
    # здесь не лечится повтором (тот принимает его за человеческий), поэтому обрыв на каждой их
    # записи проверяется отдельно, а не «вдруг попадёт в выборку».
    single = ("aurora.config.yaml", ".gitignore", ".gitattributes", "aurora_version.txt",
              "kit_path.txt")

    BEST_EFFORT = (".gitignore", ".gitattributes")

    def name_of(path: str) -> str:
        return re.sub(r"\.\d+\.aurora-tmp$", "", path.replace("\\", "/")).rsplit("/", 1)[-1]

    at_single = [i + 1 for i, path in enumerate(ops) if name_of(path) in single]
    for name in ("aurora.config.yaml", ".gitignore", ".gitattributes"):
        assert any(name_of(path) == name for path in ops), \
            f"обновление «старого мира» не пишет {name}: обрыв на этой записи нечем проверять"
    old_stamp = _stamp(base)
    points = sorted({1, 2, total // 4, total // 2, 3 * total // 4, total - 1, total, *at_single})
    for torn in (False, True):
        for n in points:
            root = tmp / f"cut-{int(torn)}-{n}"
            shutil.copytree(base, root, symlinks=True)
            before = _tree(root)
            fired: list = []
            _count, err = _run_in_process(AU, root, fired=fired, at=n, torn=torn)
            # Обрыв обязан случиться: без этого сорванная или мёртвая инъекция сбоев давала бы
            # зелёный тест на любой записи.
            assert fired == [True], f"обрыв на записи {n}/{total} не сработал: инъекция сбоев мертва"
            # `.gitignore` и `.gitattributes` кит пишет «по возможности» (отказ записи не
            # останавливает обновление), поэтому обрыв на них исключением не кончается — и штамп
            # тогда по праву ставится; повтор всё равно обязан довести дерево до того же. Любая
            # другая запись обязана кончиться отказом.
            if name_of(ops[n - 1]) in BEST_EFFORT:
                assert err is None or isinstance(err, OSError), \
                    f"обрыв на записи {n}/{total} кончился не отказом записи: {str(repr(err))[:300]}"
            else:
                assert isinstance(err, OSError), \
                    f"обрыв на записи {n}/{total} ({name_of(ops[n - 1])}) не дошёл до движка: {str(repr(err))[:300]}"
            if err is not None and n < total:
                assert _stamp(root) == old_stamp, \
                    f"обрыв на {n}/{total}: проект уже назвался обновлённым ({_stamp(root)})"
            # Файл ни прежний, ни новый — обрезок. Без обрыва посреди записи их быть не должно, с
            # обрывом — не больше одного (того, что писался), и не из числа единственных.
            broken = sorted(rel for rel, h in _tree(root).items()
                            if h != before.get(rel) and h != reference.get(rel))
            assert len(broken) <= (1 if torn else 0), \
                f"обрыв на {n}/{total} (torn={torn}): побито больше одного файла: {broken[:6]}"
            assert not [rel for rel in broken if name_of(rel) in single], \
                f"обрыв на {n}/{total}: единственный файл проекта остался обрезком: {broken}"
            _count, err = _run_in_process(AU, root)
            assert err is None, f"повторное обновление после обрыва на {n}/{total} упало: {str(repr(err))[:300]}"
            got = _tree(root)
            diff = sorted(k for k in set(got) | set(reference) if got.get(k) != reference.get(k))
            assert not diff, f"после обрыва на {n}/{total} (torn={torn}) и повтора проект не сошёлся: {diff[:6]}"
            shutil.rmtree(root, ignore_errors=True)


@test
def test_lifecycle_two_updates_at_once_converge(tmp: Path):
    """Два обновления одного проекта разом (кнопка в панели и Cron, два окна) не ломают друг друга.

    Второе обновление целиком проходит в тот миг, когда первое делает n-ю запись, — и первое
    продолжает по уже устаревшему плану (файл, который оно собиралось удалить, уже удалён).
    Оба кончаются без ошибки, проект сходится к состоянию непрерывного прогона. Затем то же
    самое настоящими процессами: два `update --apply` в одну секунду.
    """
    AU = _au()
    base = _aged_project(tmp)
    ref = tmp / "ref"
    shutil.copytree(base, ref, symlinks=True)
    total, err = _run_in_process(AU, ref)
    assert err is None, repr(err)[:300]
    reference = _tree(ref)

    for n in sorted({1, 2, 3, total // 3, 2 * total // 3, total - 1}):
        root = tmp / f"race-{n}"
        shutil.copytree(base, root, symlinks=True)

        def second(root=root):
            _c, e = _run_in_process(AU, root)
            assert e is None, f"второе обновление упало: {str(repr(e))[:300]}"

        _c, err = _run_in_process(AU, root, at=n, hook=second)
        assert err is None, f"первое обновление упало, когда второе прошло посреди него (запись {n}/{total}): {str(repr(err))[:300]}"
        got = _tree(root)
        diff = sorted(k for k in set(got) | set(reference) if got.get(k) != reference.get(k))
        assert not diff, f"два обновления разом (запись {n}/{total}) не сошлись: {diff[:6]}"
        shutil.rmtree(root, ignore_errors=True)

    if sys.platform == "win32" and os.environ.get("AURORA_TEST_PARALLEL_WIN") != "1":
        # Настоящие процессы на Windows проверяются только в неблокирующем задании windows-314
        # (AURORA_TEST_PARALLEL_WIN=1): replace_file повторяет подмену около 7 секунд, но причина
        # возможных падений там не подтверждена. Причину называют по итогу этого задания, а не
        # заранее. Гонку двух писателей без помехи системы проверяют части выше и test_folder_guides_*.
        return
    root = tmp / "procs"
    shutil.copytree(base, root, symlinks=True)
    _own_home()
    cmd = [sys.executable, str(KIT / "scripts/aurora_update.py"), str(root), "--apply"]
    procs = [subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
             for _ in range(2)]
    outs = [p.communicate(timeout=120) for p in procs]
    for p, (o, e) in zip(procs, outs):
        assert p.returncode == 0, f"параллельное обновление: rc={p.returncode}\n{o[-400:]}\n{e[-600:]}"
        assert "Traceback" not in e, e[-600:]
    got = _tree(root)
    diff = sorted(k for k in set(got) | set(reference) if got.get(k) != reference.get(k))
    assert not diff, f"два процесса разом не сошлись: {diff[:6]}"


@test
def test_folder_guides_two_writers_never_share_a_temp_file(tmp: Path):
    """Два писателя описаний папок: пока первый между записью и подменой, второй проходит целиком.

    Настоящие процессы попадают в это окно лишь иногда (проверка не должна зависеть от удачи),
    поэтому окно держим сами: подмена файла первого писателя вызывает второго (другой pid).
    С общим именем временного файла второй уносил файл первого, и первый падал на подмене.
    Остаться должен целый файл, а временных файлов — ни одного.
    """
    sys.path.insert(0, str(SCRIPTS))
    import folder_guides as FG
    root = tmp / "p"
    rel = "Raw/README.md"
    first, second = [(rel, "первый\n", "create")], [(rel, "второй\n", "create")]
    real_replace, real_getpid = os.replace, os.getpid
    state = {"inner": False}

    def replace(src, dst, *a, **kw):
        if not state["inner"]:
            state["inner"] = True
            os.getpid = lambda: real_getpid() + 1          # «другой процесс»
            try:
                FG.apply(root, second)
            finally:
                os.getpid = real_getpid
        return real_replace(src, dst, *a, **kw)

    os.replace = replace
    try:
        FG.apply(root, first)
    finally:
        os.replace, os.getpid = real_replace, real_getpid
    assert state["inner"], "второй писатель не вызывался: окно не воспроизведено"
    assert (root / rel).read_text(encoding="utf-8") in ("первый\n", "второй\n"), "файл побит"
    assert not list(root.rglob("*.aurora-tmp")), f"остались временные файлы: {list(root.rglob('*.aurora-tmp'))}"


@test
def test_lifecycle_update_structure_only_creates_folders_and_leaves_engine(tmp: Path):
    """`update --structure-only`: недостающие папки и описания и штамп версии, движок не тронут.

    Этот режим советует `aurora_doctor` как лечение, и у него свой путь записи. Повторный запуск
    ничего не меняет, а без `--apply` ничего не пишется.
    """
    root = _aged_project(tmp)
    scripts = root / ".aurora/scripts"
    stale = sorted(f for f in scripts.glob("*.py") if f.read_text(encoding="utf-8") == "# старая копия\n")
    assert stale, "в состаренном проекте нет устаревших копий движка"
    assert not (root / "Deliverables/_archive").exists()
    (root / "Raw/README.md").unlink(missing_ok=True)
    stamp = _stamp(root)
    before = _tree(root)

    dry = _py(KIT / "scripts/aurora_update.py", str(root), "--structure-only", cwd=root)
    assert "Ничего не создано" in dry.stdout, dry.stdout[-400:]
    assert _tree(root) == before, "dry-run режима «только структура» изменил проект"

    out = _py(KIT / "scripts/aurora_update.py", str(root), "--structure-only", "--apply", cwd=root)
    assert "Движок не тронут" in out.stdout, out.stdout[-400:]
    assert (root / "Deliverables/_archive").is_dir(), "недостающая папка схемы не создана"
    assert (root / "Raw/README.md").is_file(), "описание папки не создано"
    assert _stamp(root) == (KIT / "VERSION").read_text(encoding="utf-8").strip() != stamp
    assert all(f.read_text(encoding="utf-8") == "# старая копия\n" for f in stale), "режим тронул движок"
    assert not (scripts / "aurora_doctor.py").exists(), "режим доложил пропавший скрипт движка"
    assert (scripts / "kb_queue.py").exists(), "режим убрал выведенный скрипт: это дело полного обновления"

    after = _tree(root)
    again = _py(KIT / "scripts/aurora_update.py", str(root), "--structure-only", "--apply", cwd=root)
    assert "Структура актуальна" in again.stdout, again.stdout[-400:]
    assert _tree(root) == after, "повторный запуск изменил проект"


@test
def test_lifecycle_update_survives_files_that_are_not_utf8(tmp: Path):
    """Конфиг, список исключений и README папки не в UTF-8 (обрезок убитого процесса, правка в cp1251) обновление не роняют.

    Читаются они «с заменой», а писать обратно то, что не прочитано как есть, нельзя: файл
    остаётся байт в байт, как был.
    """
    root = _aged_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_bytes(cfg.read_bytes() + "\n# Старый".encode("utf-8")[:-1])
    ign = root / "aurora.update_ignore.txt"
    ign.write_bytes("# мой список\n".encode("cp1251") + b"Templates/*\n")
    # README папки схемы, сохранённый в cp1251: «чтение с заменой» стёрло бы буквы навсегда
    guide = root / "Raw/README.md"
    guide.write_bytes("# Моё описание папки Raw\nТекст человека\n".encode("cp1251"))
    keep = {p: p.read_bytes() for p in (cfg, ign, guide)}
    _update(KIT, root)
    for p, data in keep.items():
        assert p.read_bytes() == data, f"{p.name}: обновление переписало файл не в UTF-8"
    assert _stamp(root) == (KIT / "VERSION").read_text(encoding="utf-8").strip()


@test
def test_lifecycle_update_keeps_mode_of_rewritten_files(tmp: Path):
    """Исполняемый скрипт человека после переписывания путей остаётся исполняемым (0755 → 0755).

    `relayout_rewrite` правит пути `.opencode/` в `Scripts/*.sh`; временный файл рождается с
    правами по умолчанию, и без переноса прав скрипт переставал запускаться (в git проекта —
    смена режима 100755 на 100644). Тот же хелпер пишет и остальные файлы проекта.
    """
    if sys.platform == "win32":
        return                                   # бита исполнения там нет
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import write_text_atomic
    one = tmp / "one.sh"
    one.write_text("старое\n", encoding="utf-8")
    one.chmod(0o750)
    write_text_atomic(one, "новое\n")
    assert one.read_text(encoding="utf-8") == "новое\n" and one.stat().st_mode & 0o777 == 0o750, \
        "write_text_atomic потерял права прежнего файла"
    fresh = tmp / "fresh.txt"
    write_text_atomic(fresh, "с нуля\n")           # прежнего файла нет — права по умолчанию, без ошибки
    assert fresh.read_text(encoding="utf-8") == "с нуля\n"

    root = _aged_project(tmp)
    run = root / "Scripts/run.sh"
    run.parent.mkdir(exist_ok=True)
    run.write_text("#!/bin/sh\npython3 .opencode/scripts/kb_lint.py\n", encoding="utf-8")
    run.chmod(0o755)
    _update(KIT, root)
    assert ".opencode/" not in run.read_text(encoding="utf-8"), "обновление не переписало пути скрипта"
    assert run.stat().st_mode & 0o777 == 0o755, \
        f"скрипт человека потерял бит исполнения: {oct(run.stat().st_mode & 0o777)}"


@test
def test_install_gitignore_merge_works_with_the_old_common_of_a_project_copy(tmp: Path):
    """Слияние `.gitignore` не зависит от новых имён в `aurora_common`.

    Обновление, запущенное копией `update` из проекта (так его запускает панель), уже держит
    проектный `aurora_common` — прежний, без `write_text_atomic`. Верхний импорт этого имени в
    `install_aurora` ронял модуль, а `update` глотает сбой: правила кита не дописывались вовсе.
    """
    text = (SCRIPTS / "aurora_common.py").read_text(encoding="utf-8")
    cut = re.sub(r"(?s)def write_text_atomic\(.*?(?=\ndef _win_pid_alive)", "", text)
    assert cut != text and "def write_text_atomic" not in cut, "прежний aurora_common не воспроизведён"
    old = tmp / "old_scripts"
    old.mkdir()
    (old / "aurora_common.py").write_text(cut, encoding="utf-8")
    target = tmp / ".gitignore"
    target.write_text("# мой список\n.DS_Store\n", encoding="utf-8")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import aurora_common; "
            "assert not hasattr(aurora_common, 'write_text_atomic'); "
            "sys.path.insert(0, sys.argv[2]); import install_aurora; "
            "print(len(install_aurora.merge_gitignore(sys.argv[3])))")
    cp = subprocess.run([sys.executable, "-c", code, str(old), str(SCRIPTS), str(target)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(tmp))
    assert cp.returncode == 0, f"слияние упало на прежнем aurora_common:\n{cp.stderr[-600:]}"
    assert int(cp.stdout.strip()) > 3, cp.stdout
    got = target.read_text(encoding="utf-8")
    assert got.startswith("# мой список\n.DS_Store\n") and "*.aurora-tmp" in got, got[:300]


@test
def test_lifecycle_update_from_project_copy_of_a_past_release_adds_gitignore_rules(tmp: Path):
    """Проект прежнего выпуска обновляется СВОЕЙ копией `update` (через `kit_path.txt`) и получает правила кита.

    Без тегов (мелкий клон) проверка пропускается; в проверке GitHub теги обязательны.
    """
    tag = "v1.169.0"
    if tag not in _tags():
        assert os.environ.get("AURORA_REQUIRE_TAGS") != "1", f"в проверке нет тега {tag}"
        return
    root = _new_project(_tagged_kit(tag, tmp / "kit-old"), tmp / "p")
    (root / ".aurora/kit_path.txt").write_text(str(KIT) + "\n", encoding="utf-8")
    ignore = root / ".gitignore"
    before = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    assert "*.aurora-tmp" not in before
    cp = _py(root / ".aurora/scripts/aurora_update.py", str(root), "--apply", cwd=root)
    assert "Traceback" not in cp.stderr, cp.stderr[-400:]
    got = ignore.read_text(encoding="utf-8")
    assert "*.aurora-tmp" in got, "правила кита не дописаны: слияние молча упало в копии update из проекта"
    assert _stamp(root) == (KIT / "VERSION").read_text(encoding="utf-8").strip()


@test
def test_lifecycle_update_does_not_write_garbage_from_a_non_utf8_project_name(tmp: Path):
    """Имя проекта в конфиге не в UTF-8: мусора (U+FFFD) нет ни в выводе, ни в AGENTS.md.

    Прежде такой конфиг ронял обновление (громкий отказ); чтение «с заменой» убрало падение,
    но пустило замену в имя, а оно пишется в файлы, которые `update` переписывает.
    """
    root = _aged_project(tmp, "Old")
    cfg = root / "aurora.config.yaml"
    raw = cfg.read_bytes()
    assert b'name: "Old"' in raw, "в конфиге нет name: \"Old\""
    cfg.write_bytes(raw.replace(b'name: "Old"', 'name: "Проект"'.encode("cp1251"), 1))
    keep = cfg.read_bytes()
    out = _update(KIT, root)
    assert "\ufffd" not in out.stdout, f"в выводе мусор из имени проекта: {out.stdout[:300]}"
    for rel in ("AGENTS.md",):
        assert "\ufffd" not in (root / rel).read_text(encoding="utf-8", errors="replace"), \
            f"в {rel} попала замена вместо имени проекта"
    assert cfg.read_bytes() == keep, "обновление переписало конфиг не в UTF-8"


@test
def test_lifecycle_rerun_after_interrupted_run_finishes_the_tail_steps(tmp: Path):
    """Прогон оборван сразу после записи движка (штамп не поставлен): повтор доделывает и хук.

    Повтор видит «движок уже актуален» и раньше лишь досылал правила `.gitignore` и штамп:
    хук с устаревшими путями оставался, и коммит проекта вызывал несуществующий линтер.
    """
    AU = _au()
    root = _aged_project(tmp, "Старый проект", old_world=True)
    _count, err = _run_in_process(AU, root)
    assert err is None, repr(err)[:300]
    hook = root / ".git/hooks/pre-commit"
    good = hook.read_text(encoding="utf-8")
    assert ".aurora/scripts/kb_lint.py" in good, "хук не поставлен обновлением: проверять нечем"
    hook.write_text(good.replace(".aurora/scripts/", ".opencode/scripts/"), encoding="utf-8")
    (root / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text("1.100.0\n", encoding="utf-8")
    out = _update(KIT, root)
    assert "уже актуальны" in out.stdout, out.stdout[-400:]
    assert hook.read_text(encoding="utf-8") == good, "повтор после обрыва не обновил хук"
    assert _stamp(root) == (KIT / "VERSION").read_text(encoding="utf-8").strip()


@test
def test_check_release_reports_each_mismatch(tmp: Path):
    """`tests/check_release.py` сам проверен: согласованное дерево зелёное, каждое расхождение — красное.

    Он стоял на пути каждого выпуска, но проверялся только тем, что CI его запускает: сломанный
    сам, он молча пропустил бы выпуск без записи в CHANGELOG.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_release_under_test", KIT / "tests/check_release.py")
    CR = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(CR)

    def tree(version="1.2.3", changelog="## 1.2.3 — x\n", ui="1.2.3", wf="engine-tests",
             waits="engine-tests") -> Path:
        root = tmp / f"rel-{abs(hash((version, changelog, ui, wf, waits)))}"
        for rel, text in (("VERSION", version + "\n"), ("CHANGELOG.md", changelog),
                          ("cockpit/ui/panel.js", f'const UI_VERSION = "{ui}";\n'),
                          (".github/workflows/test.yml", f"name: {wf}\non: [push]\n"),
                          (".github/workflows/release.yml", f"on:\n  workflow_run:\n    workflows: [{waits}]\n")):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return root

    def check(root: Path):
        CR.ROOT = str(root)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = CR.main()
        return rc, buf.getvalue()

    rc, out = check(tree())
    assert rc == 0 and "согласованы" in out, out
    for label, kw, needle in (
            ("версия не X.Y.Z", dict(version="1.2", changelog="## 1.2 — x\n", ui="1.2"), "не вида X.Y.Z"),
            ("нет записи в CHANGELOG", dict(changelog="## 1.2.2 — x\n"), "нет записи"),
            ("панель врёт", dict(ui="1.2.2"), "UI_VERSION"),
            ("release.yml ждёт другое", dict(waits="other"), "release.yml ждёт")):
        rc, out = check(tree(**kw))
        assert rc == 1 and needle in out, (label, rc, out)
