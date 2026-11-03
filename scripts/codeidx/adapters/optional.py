#!/usr/bin/env python3
"""optional.py — необязательные адаптеры извлечения: graphify, tree-sitter, ast-grep, lizard.

Пакеты не входят в кит и не импортируются. Слот включается командой в окружении:

  AURORA_CODE_GRAPHIFY='python3 /path/adapter.py'
  AURORA_CODE_TREESITTER='...'
  AURORA_CODE_ASTGREP='...'
  AURORA_CODE_LIZARD='...'

Команда получает JSON `{path,text,lang,role}` в stdin и отвечает JSON/JSONL по контракту
`external.py`. Нет команды — слот молчит, индекс строится на stdlib. Ошибка одного слота
не отменяет остальные и не роняет файл.
"""
from __future__ import annotations

import os
import shlex

SLOTS = ("graphify", "tree-sitter", "ast-grep", "lizard")
_ENV = {"graphify": "AURORA_CODE_GRAPHIFY", "tree-sitter": "AURORA_CODE_TREESITTER",
        "ast-grep": "AURORA_CODE_ASTGREP", "lizard": "AURORA_CODE_LIZARD"}


def command(slot: str) -> list:
    """Команда слота из окружения; пусто — слот выключен."""
    raw = os.environ.get(_ENV.get(slot, ""), "").strip()
    return shlex.split(raw) if raw else []


def enabled() -> list:
    return [slot for slot in SLOTS if command(slot)]


def extract(slot: str, path: str, text: str, lang: str, role: str) -> list:
    """Записи одного слота; выключенный слот — пустой список."""
    cmd = command(slot)
    if not cmd:
        return []
    from . import external
    try:
        return external.run(cmd, path=path, text=text, lang=lang, role=role,
                            source="adapter:" + slot)
    except external.AdapterError:
        return []


def all(path: str, text: str, lang: str, role: str) -> list:
    """Записи всех включённых слотов; ошибка слота уже проглочена в `extract`."""
    out = []
    for slot in enabled():
        out.extend(extract(slot, path, text, lang, role))
    return out
