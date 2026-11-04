#!/usr/bin/env python3
"""merge_manifest.py — трёхсторонний merge `AuroraKnowledgeDB/meta/manifest.json`.

Git вызывает этот драйвер при конфликте манифеста разбора. Манифест — не текст, а
учёт источников: два участника почти всегда меняют разные ключи `sources`, поэтому
обычный построчный merge даёт конфликт на каждой параллельной правке.

Правила:
  • `sources` — по ключам. Если сторона не меняла запись, побеждает изменение другой;
    если обе меняли — запись с более поздним `processed`; при равенстве — наша.
    Удаление при неизменной второй стороне сохраняется.
  • `cards` — объединение по каноническому JSON, детерминированная сортировка.
  • `updated` — максимум, `version` — максимум; остальные top-level поля — по тому же
    правилу «кто менял».
  • Битый JSON — код 1: Git оставляет конфликт, данные не теряются.

  python3 .aurora/scripts/merge_manifest.py %O %A %B %L %P
"""
from __future__ import annotations

import json
import sys

try:  # защита версии Python: на старом интерпретаторе — понятный текст, не traceback
    import aurora_common  # noqa: F401
except ImportError:
    pass

from merge_common import read_json, write_json


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _newer(left, right) -> bool:
    """Левая запись новее правой по `processed`/`updated`/`mtime`; иначе False."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in ("processed", "updated", "mtime", "date"):
        a, b = str(left.get(key) or ""), str(right.get(key) or "")
        if a != b:
            return a > b
    return False


def _merge_sources(base: dict, ours: dict, theirs: dict) -> dict:
    out = {}
    for key in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(key), ours.get(key), theirs.get(key)
        if o == t:
            value = o
        elif o == b:
            value = t
        elif t == b:
            value = o
        elif o is None:
            value = t
        elif t is None:
            value = o
        elif _newer(o, t):
            value = o
        else:
            value = t
        if value is not None:
            out[key] = value
    return out


def _card_key(item) -> str:
    """Стабильный ключ карточки: id/name/path; пусто — объединяем по содержимому."""
    if isinstance(item, dict):
        for key in ("id", "name", "path"):
            value = item.get(key)
            if value:
                return str(value)
    return ""


def _newer_card(left, right) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    for key in ("updated", "processed", "at", "date", "mtime"):
        a, b = str(left.get(key) or ""), str(right.get(key) or "")
        if a != b:
            return a > b
    return False


def _pick_card(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if ours is None:
        return theirs
    if theirs is None:
        return ours
    return ours if _newer_card(ours, theirs) else theirs


def _merge_cards(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if not isinstance(ours, list) or not isinstance(theirs, list):
        return ours
    base = base if isinstance(base, list) else []
    base_map = {_card_key(i): i for i in base if _card_key(i)}
    ours_map = {_card_key(i): i for i in ours if _card_key(i)}
    theirs_map = {_card_key(i): i for i in theirs if _card_key(i)}
    out = {}
    for key in sorted(set(base_map) | set(ours_map) | set(theirs_map)):
        value = _pick_card(base_map.get(key), ours_map.get(key), theirs_map.get(key))
        if value is not None:
            out[key] = value
    unkeyed = {_canonical(i): i for i in [*ours, *theirs] if not _card_key(i)}
    result = list(out.values()) + [unkeyed[k] for k in sorted(unkeyed)]
    result.sort(key=lambda item: (_card_key(item), _canonical(item)))
    return result


def _merge_value(base, ours, theirs):
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if isinstance(ours, dict) and isinstance(theirs, dict):
        return _merge_dict(base if isinstance(base, dict) else {}, ours, theirs)
    return ours


def _merge_dict(base: dict, ours: dict, theirs: dict) -> dict:
    out = {}
    for key in sorted(set(base) | set(ours) | set(theirs)):
        if key == "sources":
            out[key] = _merge_sources(base.get(key) or {}, ours.get(key) or {}, theirs.get(key) or {})
        elif key == "cards":
            out[key] = _merge_cards(base.get(key), ours.get(key), theirs.get(key))
        elif key == "version":
            values = [v for v in (base.get(key), ours.get(key), theirs.get(key)) if isinstance(v, int)]
            out[key] = max(values) if values else ours.get(key, theirs.get(key))
        elif key == "updated":
            values = [str(v) for v in (base.get(key), ours.get(key), theirs.get(key)) if v]
            out[key] = max(values) if values else ours.get(key, theirs.get(key))
        else:
            out[key] = _merge_value(base.get(key), ours.get(key), theirs.get(key))
    return out


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        print("merge_manifest: нужны base ours theirs", file=sys.stderr)
        return 1
    base_path, ours_path, theirs_path = args[0], args[1], args[2]
    try:
        base = read_json(base_path, "манифест")
        ours = read_json(ours_path, "манифест")
        theirs = read_json(theirs_path, "манифест")
    except (OSError, ValueError) as exc:
        print(f"merge_manifest: {exc}", file=sys.stderr)
        return 1
    merged = _merge_dict(base, ours, theirs)
    write_json(ours_path, merged, ".manifest-merge-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
