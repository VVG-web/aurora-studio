#!/usr/bin/env python3
"""scan.py — инвентарь репозитория по объектам git, без рабочего дерева.

Почему из объектов, а не из файлов на диске: `core.autocrlf`, символические ссылки, регистр
имён и длинные пути Windows дают разный результат на разных машинах. `git ls-tree` и
`git cat-file` дают один и тот же набор байт везде, где есть git.

Повторный прогон не читает файл, у которого тот же blob и та же версия инвентаря: строка
берётся из индекса. Пакеты фиксируются транзакциями, поэтому прерванный прогон продолжается
с места остановки, а не начинается заново.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from . import model
except ImportError:  # запуск файла напрямую из scripts/codeidx
    import model  # type: ignore

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

BATCH = 200                        # файлов в одном пакете чтения и транзакции
GIT_TIMEOUT = 300


class GitError(RuntimeError):
    """git не смог прочитать репозиторий: это отказ, а не пустой индекс."""


class ScanRefused(RuntimeError):
    """Индекс не строится целиком: причина словами, без молчаливой части."""


@dataclass
class TreeEntry:
    mode: str
    kind: str          # blob, tree, commit (подмодуль)
    oid: str
    path: str


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C", LANG="C")
    return env


def _run_git(path: Path, args: list, input_bytes: bytes | None = None, timeout: int = GIT_TIMEOUT):
    try:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            input=input_bytes, capture_output=True, timeout=timeout, env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitError(f"git {' '.join(args)}: {e}") from e


def _mask_rx(mask: str):
    """Маска пути в регулярное выражение: `**` — любые каталоги, `*` — в пределах имени."""
    mask = model.posix(mask.strip())
    if not mask:
        return None
    out = re.escape(mask)
    out = out.replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    out = out.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    if "/" not in mask:
        out = "(?:.*/)?" + out
    return re.compile("^" + out + "(?:/.*)?$")


def _matches(rel: str, rxs: list) -> bool:
    return any(rx and rx.match(rel) for rx in rxs)


class GitRepo:
    """Тонкая обёртка над git: дерево ревизии и поток содержимого объектов."""

    def __init__(self, path):
        self.path = Path(path)
        cp = _run_git(self.path, ["rev-parse", "--show-toplevel"])
        if cp.returncode:
            raise GitError(f"{self.path}: это не репозиторий git ({cp.stderr.decode('utf-8', 'replace').strip()})")
        self.root = Path(cp.stdout.decode("utf-8", "replace").strip())

    def head(self) -> str:
        cp = _run_git(self.path, ["rev-parse", "-q", "--verify", "HEAD^{commit}"])
        if cp.returncode:
            raise GitError(f"{self.path}: нет HEAD — репозиторий пуст")
        return cp.stdout.decode("ascii", "replace").strip()

    def is_ancestor(self, old: str, new: str) -> bool:
        """Старый коммит — предок нового. Переписанная история даёт False."""
        cp = _run_git(self.path, ["merge-base", "--is-ancestor", old, new])
        return cp.returncode == 0

    def count_between(self, old: str, new: str) -> int | None:
        """Сколько коммитов от старого до нового; None — посчитать не удалось."""
        cp = _run_git(self.path, ["rev-list", "--count", f"{old}..{new}"])
        if cp.returncode:
            return None
        try:
            return int(cp.stdout.decode("ascii", "replace").strip())
        except ValueError:
            return None

    def ls_tree(self, rev: str = "HEAD") -> list:
        cp = _run_git(self.path, ["ls-tree", "-r", "-z", "--full-tree", rev])
        if cp.returncode:
            raise GitError(f"{self.path}: не читается дерево {rev}: {cp.stderr.decode('utf-8', 'replace').strip()}")
        out = []
        for chunk in cp.stdout.split(b"\0"):
            if not chunk:
                continue
            head, _, raw_path = chunk.partition(b"\t")
            parts = head.split()
            if len(parts) < 3:
                continue
            mode, kind, oid = parts[0].decode("ascii", "replace"), parts[1].decode("ascii", "replace"), parts[2].decode("ascii", "replace")
            path = raw_path.decode("utf-8", "replace")
            out.append(TreeEntry(mode=mode, kind=kind, oid=oid, path=model.posix(path)))
        return out

    def attrs(self, paths: list) -> dict:
        """`linguist-generated` и `linguist-vendored` из `.gitattributes`; ошибка git — пусто."""
        if not paths:
            return {}
        cp = _run_git(self.path, ["check-attr", "-z", "--stdin", "linguist-generated", "linguist-vendored"],
                      input_bytes="\0".join(paths).encode("utf-8") + b"\0")
        if cp.returncode:
            return {}
        fields = cp.stdout.split(b"\0")
        out = {}
        for i in range(0, len(fields) - 2, 3):
            path = fields[i].decode("utf-8", "replace")
            attr = fields[i + 1].decode("ascii", "replace")
            value = fields[i + 2].decode("ascii", "replace")
            if value not in ("set", "true", "1"):
                continue
            out.setdefault(path, set()).add(attr)
        return out

    def cat_file_batch(self, oids):
        """Поток `(oid, bytes|None)` через `git cat-file --batch`.

        Запись oid'ов идёт в отдельном потоке: иначе на большом репозитории заполняется
        труба stdout, и мы ждём друг друга. Читаем по одному объекту, память не растёт.
        """
        oids = list(oids)
        proc = subprocess.Popen(
            ["git", "-C", str(self.path), "cat-file", "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_git_env(),
        )
        import threading

        def feed():
            try:
                for oid in oids:
                    proc.stdin.write((oid + "\n").encode("ascii"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=feed, daemon=True)
        writer.start()
        try:
            for _ in oids:
                header = proc.stdout.readline()
                if not header:
                    break
                parts = header.split()
                if len(parts) >= 2 and parts[1] == b"missing":
                    yield parts[0].decode("ascii", "replace"), None
                    continue
                if len(parts) < 3:
                    yield "", None
                    continue
                size = int(parts[2])
                data = proc.stdout.read(size)
                proc.stdout.read(1)  # завершающий перевод строки после данных
                yield parts[0].decode("ascii", "replace"), data
        finally:
            writer.join(timeout=5)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def module_of(rel: str) -> str:
    """Модуль по пути: `src/main/java/...` — пакет, иначе первый осмысленный каталог."""
    parts = model.posix(rel).split("/")
    for marker in ("src/main/java", "src/main/kotlin", "src/main", "src", "app", "packages", "modules", "lib"):
        bits = marker.split("/")
        if parts[:len(bits)] == bits and len(parts) > len(bits) + 1:
            rest = parts[len(bits):-1]
            return ".".join(rest[:4]) if marker.startswith("src/main/java") else rest[0]
    return parts[0] if len(parts) > 1 else ""


def index_priority(rel: str, role: str) -> tuple:
    """Порядок разбора при пределе: сначала точки входа и исходники, потом тесты и документы."""
    low = model.posix(rel).lower()
    entry = any(x in low for x in ("src/main", "/main/", "cmd/", "app/", "internal/", "pkg/"))
    return (0 if entry else 1 if role == "src" else 2 if role in ("test", "migration") else 3, low)


def _record(path: str, oid: str, role: str, reason: str, skip: str, size: int = 0) -> model.FileRecord:
    return model.FileRecord(path=path, blob=oid, lang=model.language_of(path), role=role,
                            module=module_of(path), size=size, skipped=skip, role_reason=reason)


def _cached(store, path: str, oid: str, role: str):
    row = store.get_file(path)
    if not row:
        return None
    if row["blob"] != oid or row["extractor_ver"] != model.INVENTORY_VER or row["role"] != role:
        return None
    return row


def _scan_one(path: str, oid: str, data: bytes, store, stats: model.ScanStats) -> model.FileRecord:
    """Разобрать один файл: роль, кодировка, строки, секреты. Значения секретов не сохраняются."""
    store.clear_secrets(path)
    skip, role, reason = model.classify(path, data[:8192])
    if skip:
        return _record(path, oid, role, reason, skip, len(data))
    text, enc = model.decode_text(data)
    text = model.normalize_newlines(text)
    hits = model.scan_text(text)
    safe_text = text
    if hits:
        safe_text, _ = model.mask_secret_lines(text, hits)
        for rule, line, mask in hits:
            store.put_secret(rule, path, line, mask)
        stats.secrets += len(hits)
    return model.FileRecord(
        path=path, blob=oid, lang=model.language_of(path), role=role, module=module_of(path),
        size=len(data), lines=model.line_count(text), enc=enc, role_reason=reason,
        text_hash=model.text_hash(safe_text),
    )


def _cached_record(row) -> model.FileRecord:
    return model.FileRecord(
        path=row["path"], blob=row["blob"], lang=row["lang"], role=row["role"],
        module=row["module"], size=row["bytes"], lines=row["lines"], enc=row["enc"],
        skipped=row["skipped"], role_reason=row["role_reason"], text_hash=row["text_hash"],
    )


def _select_entry(entry, attrs, inc, exc, store):
    """(запись или None, нужно читать содержимое, взято из кэша)."""
    if entry.kind != "blob" or entry.mode == "120000":
        skip = "submodule" if entry.kind == "commit" else "symlink"
        role = "vendor" if entry.kind == "commit" else "src"
        return _record(entry.path, entry.oid, role, skip, skip), False, False
    if exc and _matches(entry.path, exc) and not _matches(entry.path, inc):
        return _record(entry.path, entry.oid, "src", "маска exclude", "excluded"), False, False
    skip, role, reason = model.classify(entry.path)
    if entry.path in attrs:
        if "linguist-vendored" in attrs[entry.path]:
            skip, role, reason = "deps", "vendor", "linguist-vendored"
        elif "linguist-generated" in attrs[entry.path]:
            skip, role, reason = "generated", "generated", "linguist-generated"
    cached = _cached(store, entry.path, entry.oid, role)
    if cached is not None:
        return _cached_record(cached), False, True
    if skip:
        return _record(entry.path, entry.oid, role, reason, skip), False, False
    return None, True, False


def _select_entries(entries, attrs, inc, exc, store, stats) -> tuple:
    records, pending = [], []
    for entry in entries:
        rec, need_content, reused = _select_entry(entry, attrs, inc, exc, store)
        if rec is not None:
            records.append(rec)
            if not reused:
                store.put_file(rec)
                if rec.skipped:
                    store.clear_secrets(rec.path)
        if need_content:
            pending.append(entry)
        if reused:
            stats.reused += 1
    return records, pending


def _limit_pending(pending, records, store, stats) -> list:
    pending.sort(key=lambda e: index_priority(e.path, model.role_of(e.path)[0]))
    if len(pending) <= model.MAX_INDEX_FILES:
        return pending
    keep, drop = pending[:model.MAX_INDEX_FILES], pending[model.MAX_INDEX_FILES:]
    for entry in drop:
        rec = _record(entry.path, entry.oid, model.role_of(entry.path)[0],
                      "предел разбираемых файлов", "over_limit")
        records.append(rec)
        store.put_file(rec)
    stats.over_limit = len(drop)
    return keep


def _read_pending(repo, pending, store, stats, records, stop_after, progress, started) -> bool:
    """Читать пакетами; True — остановились по `stop_after`, готовые строки уже в базе."""
    for start in range(0, len(pending), BATCH):
        by_oid = {}
        for entry in pending[start:start + BATCH]:
            by_oid.setdefault(entry.oid, []).append(entry)
        for oid, data in repo.cat_file_batch(list(by_oid)):
            for entry in by_oid.get(oid, []):
                if data is None:
                    rec = _record(entry.path, oid, model.role_of(entry.path)[0],
                                  "объект не найден в git", "parse_error")
                else:
                    rec = _scan_one(entry.path, oid, data, store, stats)
                    if not rec.skipped:
                        stats.parsed += 1
                        store.put_path_terms(rec.path, model.path_words(rec.path))
                records.append(rec)
                store.put_file(rec)
                if progress:
                    progress(rec)
                if stop_after and stats.parsed >= stop_after:
                    store.commit()
                    stats.interrupted = True
                    stats.duration_s = time.time() - started
                    return True
        store.commit()
    return False


def _finish_stats(records, stats) -> None:
    stats.skipped = {}
    for rec in records:
        if rec.skipped:
            stats.skipped[rec.skipped] = stats.skipped.get(rec.skipped, 0) + 1
        else:
            stats.indexed += 1
            stats.lines += rec.lines
            stats.bytes += rec.size
    stats.languages = model.language_summary(records)


def scan_repo(repo_path, store, include: str = "", exclude: str = "", stop_after: int | None = None,
              progress=None) -> model.ScanStats:
    """Построить инвентарь одного клона. `store` уже открыт; запись пакетов — внутри.

    `stop_after` — только для проверки продолжения: после N разобранных файлов функция
    возвращает частичный итог с `interrupted=True`, не удаляя уже записанные строки.
    """
    started = time.time()
    repo = GitRepo(repo_path)
    rev = repo.head()
    entries = repo.ls_tree(rev)
    if len(entries) > model.MAX_INVENTORY_FILES:
        raise ScanRefused(f"{repo_path}: файлов {len(entries)}, предел {model.MAX_INVENTORY_FILES}. "
                          "Укажите paths/include в реестре или исключите лишнее.")
    stats = model.ScanStats(name=Path(repo_path).name, rev=rev, files=len(entries))
    attrs = repo.attrs([e.path for e in entries])
    inc = [_mask_rx(m) for m in (include or "").split(",") if m.strip()]
    exc = [_mask_rx(m) for m in (exclude or "").split(",") if m.strip()]
    records, pending = _select_entries(entries, attrs, inc, exc, store, stats)
    pending = _limit_pending(pending, records, store, stats)
    if _read_pending(repo, pending, store, stats, records, stop_after, progress, started):
        return stats
    _finish_stats(records, stats)
    stats.duration_s = time.time() - started
    store.replace_inventory(records, rev, stats)
    store.meta_set("indexed_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    store.commit()
    return stats
