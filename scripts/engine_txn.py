#!/usr/bin/env python3
"""engine_txn.py — транзакционное обновление движка проекта (BL-LCY-07): проверка до записи, снимок, журнал, откат.

Обновление движка (`aurora_update.py`) писало файлы `.aurora/` один за другим прямо поверх старых, в порядке манифеста.
Между первой и последней записью движок был смешанным (после консолидации модулей — `ImportError` на любой команде),
после записи проверки не было, а снимка не было вовсе: `.aurora/` вне git, откатить нечем. Здесь это замыкается:

  verify_overlay(staged, …)    новые тексты скриптов проверяются ДО первой записи: компилируются, их верхнеуровневые
                               импорты локальных модулей находят файл и имя в том составе, что получится после обновления
  snapshot(project, …)         копия заменяемых и удаляемых файлов движка вне проекта (`~/.aurora/engine-backups/`),
                               хранится три последних; в манифесте снимка — версия «было», хеши и список побочных эффектов
  mark_pending / clear_pending метка `.aurora/state/update.pending` создаётся ДО первой записи и снимается в конце, до штампа:
                               оборванное обновление видно (`pending()`), а не выглядит «почти новым»
  write_lock(project, …)       `.aurora/engine.lock.json`: sha256 каждого файла движка, версия кита, его sha и `ENGINE_API`
  check_installed(...)         после записи: хеш каждого файла совпадает с проверенным текстом, скрипты компилируются
  restore(project, snap)       возврат файлов из снимка (созданные обновлением — удаляются)

Модуль листовой: стандартная библиотека и `aurora_fs`; в манифесте он стоит раньше `aurora_update.py`.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aurora_fs as AFS  # noqa: E402

ENGINE = ".aurora"
LOCK_FILE = f"{ENGINE}/engine.lock.json"
PENDING = f"{ENGINE}/state/update.pending"
UPDATE_LOCK = f"{ENGINE}/state/update.lock"
BACKUP_KEEP = 3
LOCK_SCHEMA = 1
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]{0,24}")


def clean(value, limit: int = 60) -> str:
    """Значение из файла метки или манифеста снимка — для печати: без управляющих знаков и длинного хвоста."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))[:limit]


def version_text(value) -> "str | None":
    """Версия из манифеста снимка, годная к записи в штамп проекта; иначе None."""
    return value if isinstance(value, str) and _VERSION_RE.fullmatch(value.strip()) else None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """Хеш текста так, как он ляжет на диск: UTF-8, переводы строк как есть (движок пишет LF)."""
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path) -> str:
    try:
        with open(path, "rb") as fh:
            return sha256_bytes(fh.read())
    except OSError:
        return ""


def kit_sha(kit) -> str:
    """Коммит кита (`git rev-parse HEAD`); кит без git или без самого git — пусто."""
    try:
        cp = subprocess.run(["git", "-C", str(kit), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                            env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return ""
    out = cp.stdout.strip()
    return out if cp.returncode == 0 and len(out) in (40, 64) else ""


# --------------------------------------------------------------------------- проверка до записи

def _unguarded_imports(tree: ast.Module) -> list:
    """Импорты верхнего уровня модуля, не обёрнутые в `try`/`if`/функцию: именно они дают `ImportError` при запуске."""
    return [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]


def _defined_names(tree: ast.Module) -> "set | None":
    """Имена, которые модуль определяет на верхнем уровне (включая `if`/`try`/`with`). Модуль с `__getattr__` или
    `from … import *` — имён не назвать: None, сверка имён по нему пропускается."""
    out: set = set()

    def walk(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name):
                            out.add(x.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    if a.name == "*":
                        out.add("*")
                    out.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, (ast.If, ast.With, ast.AsyncWith)):
                walk(n.body)
                walk(getattr(n, "orelse", []))
            elif isinstance(n, ast.Try):
                walk(n.body)
                for h in n.handlers:
                    walk(h.body)
                walk(n.orelse)
                walk(n.finalbody)
            elif isinstance(n, (ast.For, ast.While)):
                walk(n.body)
    walk(tree.body)
    return None if ("*" in out or "__getattr__" in out) else out


def verify_overlay(staged: dict, current_scripts, kit_modules=()) -> list:
    """Новые скрипты движка годны? → список проблем (пусто — можно писать).

    `staged` — {путь от `.aurora/`: новый текст} (проверяются `scripts/*.py`); `current_scripts` — папка `scripts/` движка
    проекта сейчас (то, что останется нетронутым); `kit_modules` — имена модулей кита (`aurora_common`, …): импорт по
    такому имени — локальный. Состав после обновления = то, что лежит сейчас, поверх — новые тексты. Проверяется:
    скрипт компилируется; каждый его верхнеуровневый локальный импорт находит модуль в этом составе, а `from m import x` —
    имя `x` в верхнем уровне `m` (если оно определимо)."""
    cur = Path(current_scripts)
    names: dict = {}                                   # имя модуля → текст (или None: файл есть, читать не нужно)
    if cur.is_dir():
        for p in cur.glob("*.py"):
            names[p.stem] = None
    texts: dict = {}
    for rel, text in staged.items():
        if rel.startswith("scripts/") and rel.endswith(".py") and "/" not in rel[len("scripts/"):]:
            texts[rel[len("scripts/"):-3]] = text
    names.update({k: v for k, v in texts.items()})
    local = set(names) | set(kit_modules)
    problems, trees, cache = [], {}, {}

    def tree_of(mod):
        if mod in trees:
            return trees[mod]
        src = names.get(mod)
        if src is None:
            try:
                src = (cur / (mod + ".py")).read_text(encoding="utf-8", errors="replace")
            except OSError:
                src = None
        try:
            trees[mod] = ast.parse(src) if src is not None else None
        except SyntaxError:
            trees[mod] = None
        return trees[mod]

    for mod, text in sorted(texts.items()):
        try:
            tree = ast.parse(text, filename=mod + ".py")
            compile(tree, mod + ".py", "exec")
        except (SyntaxError, ValueError) as e:
            problems.append(f"scripts/{mod}.py: не компилируется — {e}")
            continue
        for node in _unguarded_imports(tree):
            if isinstance(node, ast.Import):
                targets = [(a.name.split(".")[0], []) for a in node.names]
            elif node.level == 0 and node.module:
                targets = [(node.module.split(".")[0], [a.name for a in node.names] if "." not in node.module else [])]
            else:
                continue
            for target, wanted in targets:
                if target not in local or target == mod:
                    continue
                if target not in names:
                    problems.append(f"scripts/{mod}.py: импортирует модуль «{target}», которого не будет в составе движка")
                    continue
                if wanted:
                    if target not in cache:
                        t = tree_of(target)
                        cache[target] = _defined_names(t) if t is not None else None
                    have = cache[target]
                    for w in wanted:
                        if have is not None and w != "*" and w not in have:
                            problems.append(f"scripts/{mod}.py: «{target}» не определяет имя «{w}», которое оно импортирует")
    return problems


# --------------------------------------------------------------------------- снимок

def backups_root() -> Path:
    """Где лежат снимки движка проектов: рядом с прочим состоянием панели, вне проектов и вне git."""
    return Path(os.path.expanduser("~")) / ".aurora" / "engine-backups"


def project_key(project) -> str:
    """Папка снимков проекта: хеш настоящего пути (на Windows без учёта регистра) и короткое имя — путь снимка не должен
    упираться в предел длины Windows."""
    real = os.path.normcase(os.path.realpath(str(project)))
    name = re.sub(r"[^0-9A-Za-z._-]+", "_", os.path.basename(real) or "project")[:16]
    return hashlib.sha1(real.encode("utf-8", "replace")).hexdigest()[:10] + "-" + name


def list_snapshots(project) -> list:
    """Снимки проекта, новые первыми: [(папка, манифест)]."""
    base = backups_root() / project_key(project)
    out = []
    try:
        names = sorted(os.listdir(base), reverse=True)
    except OSError:
        return out
    for name in names:
        man = AFS.read_state(base / name / "manifest.json", None, kind=dict, quarantine=False)
        if man is not None:
            out.append((base / name, man))
    return out


def snapshot(project, rels, meta: dict) -> Path:
    """Снять копию файлов движка `rels` (пути от корня проекта) перед обновлением. → папка снимка.

    Файла нет — в манифест снимка идёт `existed: false` (откат такой файл удалит). Хранятся три последних снимка проекта."""
    project = Path(project)
    stamp = AFS.utc_stamp().replace("-", "").replace(":", "").replace("T", "-").rstrip("Z") + "-%03d" % (int(time.time() * 1000) % 1000)
    was = re.sub(r"[^0-9A-Za-z._]+", "_", str(meta.get("from") or "x"))[:12]
    base = backups_root() / project_key(project)
    snap = base / (stamp + "-" + was)
    for d in (backups_root(), base, snap, snap / "files"):
        os.makedirs(d, mode=0o700, exist_ok=True)          # снимок несёт путь проекта: чужим пользователям он не нужен
    files = {rel: _record(project, snap, rel) for rel in rels}
    AFS.write_json(snap / "manifest.json", {**meta, "project": os.path.realpath(str(project)), "files": files,
                                            "created": AFS.utc_stamp()}, mode=0o600)
    try:
        names = sorted(os.listdir(base), reverse=True)     # лишние снимки уходят и с испорченным манифестом, а не копятся
    except OSError:
        names = []
    for old in names[BACKUP_KEEP:]:
        _rmtree(base / old)
    return snap


def _record(project: Path, snap: Path, rel: str) -> dict:
    """Запись о файле в манифесте снимка; существующий файл копируется в снимок (права 0600)."""
    src = project / rel
    if src.is_symlink():
        return {"existed": False, "sha256": "", "skipped": "ссылка"}      # общий файл: не наш, откат его не тронет
    if src.is_file():
        dst = snap / "files" / rel
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        data = src.read_bytes()
        AFS.write_atomic(dst, data, fsync=False, mode=0o600)
        return {"existed": True, "sha256": sha256_bytes(data)}
    return {"existed": False, "sha256": ""}


def own_snapshot(project, path) -> "Path | None":
    """Снимок проекта по пути из метки `update.pending`: только из папки снимков ЭТОГО проекта (метка лежит в проекте, и
    ей не доверяют вслепую); нет такого — None."""
    try:
        real = Path(os.path.realpath(str(path)))
        base = Path(os.path.realpath(str(backups_root() / project_key(project))))
    except (OSError, ValueError, TypeError):
        return None
    return real if real.parent == base and (real / "manifest.json").is_file() else None


def extend_snapshot(project, snap, rels) -> None:
    """Дополнить снимок файлами, которых в нём ещё нет: повторный запуск после обрыва продолжает ту же операцию, и откат
    ведёт к состоянию до ПЕРВОЙ попытки, а не к полусобранному."""
    project, snap = Path(project), Path(snap)
    man = AFS.read_state(snap / "manifest.json", None, kind=dict, quarantine=False)
    if not isinstance(man, dict) or not isinstance(man.get("files"), dict):
        return
    new = [rel for rel in rels if rel not in man["files"]]
    for rel in new:
        man["files"][rel] = _record(project, snap, rel)
    if new:
        AFS.write_json(snap / "manifest.json", man, mode=0o600)


def _rmtree(path) -> None:
    import shutil
    shutil.rmtree(str(path), ignore_errors=True)


def _engine_rel(rel) -> bool:
    """Путь из манифеста снимка — файл под `.aurora/` проекта: относительный, без `..`, без обратных косых и диска."""
    if not isinstance(rel, str) or not rel.startswith(ENGINE + "/") or "\\" in rel or "\x00" in rel:
        return False
    parts = rel.split("/")
    return all(p not in ("", ".", "..") and ":" not in p for p in parts)


def restore(project, snap, rels=None) -> list:
    """Вернуть файлы движка из снимка. → строки о том, что не вышло (пусто — вернулось всё). Файл, которого до обновления
    не было, удаляется; вернувшийся файл сверяется с хешем снимка. Манифест лежит вне проекта и ему не доверяют вслепую:
    путь обязан быть файлом под `.aurora/` этого проекта (без `..` и ссылок по пути), снимок — этого проекта."""
    project, snap = Path(project), Path(snap)
    man = AFS.read_state(snap / "manifest.json", None, kind=dict, quarantine=False)
    if man is None:
        return [f"снимок {snap.name}: манифест не читается"]
    if os.path.realpath(str(man.get("project") or "")) != os.path.realpath(str(project)):
        return [f"снимок {snap.name}: снят с другого проекта"]
    listing = man.get("files")
    if not isinstance(listing, dict):
        return [f"снимок {snap.name}: в манифесте нет списка файлов"]
    root = os.path.realpath(str(project / ENGINE))
    failed = []
    for rel, rec in sorted(listing.items()):
        if rels is not None and rel not in rels:
            continue
        if not isinstance(rec, dict) or not _engine_rel(rel):
            failed.append(f"{clean(rel)}: путь вне .aurora/ проекта — пропущен")
            continue
        if rec.get("skipped"):
            continue
        dst = project / rel
        if os.path.commonpath([root, os.path.realpath(str(dst.parent))]) != root:
            failed.append(f"{rel}: путь ведёт за пределы .aurora/ проекта (ссылка) — пропущен")
            continue
        try:
            if rec.get("existed"):
                data = (snap / "files" / rel).read_bytes()
                if sha256_bytes(data) != rec.get("sha256"):
                    failed.append(f"{rel}: копия в снимке повреждена (хеш не сошёлся)")
                    continue
                AFS.write_atomic(dst, data, fsync=False, follow_links=False)
            else:
                dst.unlink(missing_ok=True)
        except OSError as e:
            failed.append(f"{rel}: {e}")
    return failed


# --------------------------------------------------------------------------- метка и замок движка

def mark_pending(project, info: dict) -> None:
    """Метка пишется целиком, а ссылка на её месте заменяется сама (`follow_links=False`): подложенная ссылка на чужой
    файл не будет перезаписана."""
    AFS.write_json(Path(project) / PENDING, {**info, "started": AFS.utc_stamp(), "pid": os.getpid()},
                   follow_links=False)


def clear_pending(project) -> None:
    """Снять метку. Не снялась (диск, права) — ошибка поднимается: обновление не объявит себя законченным, пока метка висит."""
    try:
        (Path(project) / PENDING).unlink()
    except FileNotFoundError:
        pass


def read_pending(project) -> dict:
    """Метка обновления как записана, без оглядки на замок: тот, кто сам держит замок обновления, видит в ней след
    прежнего, оборвавшегося запуска."""
    return AFS.read_state(Path(project) / PENDING, {}, quarantine=False)


def pending(project) -> dict:
    """Метка оборванного обновления; пусто — обновление не прерывалось. Метка живого обновления (его замок занят)
    тоже пусто: оно ещё идёт, а не оборвано."""
    rec = read_pending(project)
    if rec and AFS.is_locked(Path(project) / UPDATE_LOCK):
        return {}
    return rec


def update_lock(project, timeout=0.5):
    """Замок обновления проекта: два обновления разом (кнопка и Cron, две панели) файлы друг у друга не перепишут. На
    файловой системе без замков ОС — без замка, с предупреждением (обновление не должно падать трассировкой)."""
    return AFS.FileLock(Path(project) / UPDATE_LOCK, timeout=timeout, info={"what": "обновление движка"}, optional=True)


def read_lock(project) -> dict:
    return AFS.read_state(Path(project) / LOCK_FILE, {}, quarantine=False)


def write_lock(project, kit, version: str, rels) -> dict:
    """Записать `.aurora/engine.lock.json`: версия кита, его коммит, `ENGINE_API` и sha256 каждого файла движка `rels`.

    Содержимое определяется только движком (без времени записи): одинаковый движок даёт одинаковый файл, повторное
    обновление его не переписывает, а у свежего и обновлённого проектов он совпадает."""
    from aurora_common import kit_engine_api       # лениво: процесс с прежним aurora_common в памяти импортирует модуль
    project = Path(project)
    files = {rel: sha256_file(project / rel) for rel in sorted(set(rels)) if (project / rel).is_file()}
    rec = {"schema": LOCK_SCHEMA, "version": version, "kit_sha": kit_sha(kit), "engine_api": kit_engine_api(kit),
           "files": files}
    if read_lock(project) != rec:
        AFS.write_json(project / LOCK_FILE, rec)
    return rec


def check_installed(project, expected: dict) -> list:
    """После записи: файл на месте, хеш совпадает с проверенным текстом, скрипт компилируется. → проблемы."""
    project, bad = Path(project), []
    for rel, want in sorted(expected.items()):
        got = sha256_file(project / rel)
        if got != want:
            bad.append(f"{rel}: на диске {'нет файла' if not got else 'не тот текст'} (хеш не сошёлся)")
        elif rel.endswith(".py"):
            try:
                compile((project / rel).read_bytes(), rel, "exec")
            except (SyntaxError, ValueError) as e:
                bad.append(f"{rel}: не компилируется — {e}")
    return bad
