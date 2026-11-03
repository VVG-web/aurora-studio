#!/usr/bin/env python3
"""Стабильная и безопасная идентичность участника для пользовательских частей состояния."""
from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import unicodedata


def _git_config(project, key: str) -> str:
    """Настройка git проекта; отсутствие git или значения не отличается от пустой строки."""
    try:
        cp = subprocess.run(
            ["git", "-C", os.fspath(project), "config", "--get", key],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _os_user() -> str:
    try:
        return getpass.getuser().strip() or "user"
    except (OSError, KeyError):
        return "user"


def _slug(value: str) -> str:
    """ASCII-slug только для читаемости файла; уникальность обеспечивает digest ключа."""
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:48].strip("-") or "user"


def actor(project, who: str = "") -> tuple[str, str]:
    """Вернуть `(actor_id, display_name)`: override, git и OS в порядке контракта."""
    override = os.environ.get("AURORA_USER", "").strip()
    explicit = str(who or "").strip()
    if override:
        display = key = override
    else:
        display = explicit or _git_config(project, "user.name") or _os_user()
        key = _git_config(project, "user.email") or display
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{_slug(display)}-{digest}", display


def shard_file(project, relative_directory, who: str = "") -> str:
    """Абсолютный путь части участника внутри проекта, без traversal через имя или папку."""
    root = Path(project).expanduser().resolve()
    raw = os.fspath(relative_directory)
    if not isinstance(raw, str):
        raise TypeError("relative_directory должен быть строковым путём")
    relative = Path(raw.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or (
            relative.parts and re.fullmatch(r"[A-Za-z]:", relative.parts[0])):
        raise ValueError("relative_directory должен оставаться внутри проекта")
    directory = (root / relative).resolve()
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise ValueError("relative_directory должен оставаться внутри проекта") from exc
    actor_id, _display = actor(project, who)
    return str(directory / f"{actor_id}.json")
