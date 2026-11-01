#!/usr/bin/env python3
"""history.py — история коммитов и ключи задач.

Читаем `git log --all --name-status -M`: SHA, дата, тема, файлы и переименования. Авторы не
сохраняются: индекс кода не должен превращаться в кадровый учёт. Ключами считаются только
известные проекту идентификаторы: иначе `UTF-8` и `SHA-256` из сообщений станут «ключами».

Переписанная история (force-push, rebase) не ошибка: индекс пересобирается целиком, старые
коммиты исчезают вместе со старым графом.
"""
from __future__ import annotations

import re

try:
    from . import model
except ImportError:  # запуск файла напрямую
    import model  # type: ignore

SRC = "git-history"
STORY_RX = re.compile(r"\bUS-\d+(?:\.\d+)*\b")
BRANCH_RX = re.compile(r"(?:Merge branch|Merge pull request|from)\s+['\"]?[\w./-]*?([A-Z][A-Z0-9]+-\d+)\b")
SEP, FIELD = "\x1e", "\x1f"


def _key_rx(keys) -> re.Pattern | None:
    known = {str(k).upper() for k in keys if str(k).strip()}
    if not known:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(known)) + r")-\d+\b")


def _keys_of(subject: str, key_rx) -> list:
    out = []
    if key_rx:
        out.extend((m.group(0), "jira") for m in key_rx.finditer(subject))
    out.extend((m.group(0), "story") for m in STORY_RX.finditer(subject))
    out.extend((m.group(1), "branch") for m in BRANCH_RX.finditer(subject))
    seen = set()
    return [(k, kind) for k, kind in out if not (k in seen or seen.add(k))]


def _parse(text: str):
    """Поток `(sha, ts, subject, [(status, path, old_path)])` из вывода `git log`."""
    for chunk in text.split(SEP):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, body = chunk.partition("\n")
        parts = head.split(FIELD)
        if len(parts) < 3:
            continue
        sha, ts, subject = parts[0], parts[1], FIELD.join(parts[2:])
        files = []
        for line in body.splitlines():
            bits = line.split("\t")
            if len(bits) == 2 and bits[0]:
                files.append((bits[0], bits[1], ""))
            elif len(bits) >= 3 and bits[0].startswith("R"):
                files.append(("R", bits[2], bits[1]))
        yield sha, ts, subject, files


def index_repo(repo, store, keys=(), rev: str = "") -> tuple:
    """Пересобрать историю репозитория. → (коммитов, ключей). Повтор на той же ревизии — из meta."""
    rev = rev or repo.head()
    signature = ",".join(sorted({str(k).upper() for k in keys if str(k).strip()}))
    if store.meta_get("history_rev") == rev and store.meta_get("history_keys") == signature:
        return int(store.meta_get("history_commits", "0") or 0), int(store.meta_get("history_keys_count", "0") or 0)
    raw = repo.run("log", "--all", "--name-status", "-M", "--format=%x1e%H%x1f%cI%x1f%s")
    text = raw.decode("utf-8", "replace")
    key_rx = _key_rx(keys)
    commits = key_count = 0
    store.clear_history()
    for sha, ts, subject, files in _parse(text):
        store.put_commit(sha, ts, subject)
        commits += 1
        for status, path, old in files:
            store.put_commit_file(sha, model.posix(path), status, model.posix(old))
        for key, kind in _keys_of(subject, key_rx):
            store.put_commit_key(sha, key, kind)
            key_count += 1
    store.meta_set("history_rev", rev)
    store.meta_set("history_keys", signature)
    store.meta_set("history_commits", commits)
    store.meta_set("history_keys_count", key_count)
    store.commit()
    return commits, key_count
