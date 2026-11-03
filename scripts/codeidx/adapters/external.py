#!/usr/bin/env python3
"""external.py — контракт необязательного адаптера извлечения кода.

Адаптер — отдельный процесс: получает JSON `{path,text,lang,role}` в stdin и отдаёт
JSON-массив или JSONL записей схемы v1 (`symbol`, `edge`, `route`, `table`, `column`,
`topic`, `config`, `string`, `test`, `reqtag`). Ядро не импортирует пакеты адаптера и не
доверяет его выводу: проверяет тип, вид записи, лимит и таймаут. Любая ошибка означает
«адаптер не дал результата», и вызывающий остаётся на stdlib.
"""
from __future__ import annotations

import json
import subprocess

ALLOWED = frozenset({"symbol", "edge", "route", "table", "column", "topic", "config",
                     "string", "test", "reqtag"})
DEFAULT_TIMEOUT = 20.0
DEFAULT_LIMIT = 5000


class AdapterError(Exception):
    """Адаптер не ответил или ответил не по контракту."""


def _records(stdout: str, limit: int) -> list:
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                data.append(json.loads(line))
            except ValueError as exc:
                raise AdapterError(f"строка не JSON: {exc}") from exc
    if not isinstance(data, list):
        raise AdapterError("ответ не JSON-массив и не JSONL")
    if len(data) > limit:
        raise AdapterError(f"записей больше лимита {limit}")
    out = []
    for rec in data:
        if not isinstance(rec, dict):
            raise AdapterError("запись не объект")
        kind = rec.get("rec")
        if kind not in ALLOWED:
            raise AdapterError(f"неизвестный вид записи: {kind!r}")
        out.append(rec)
    return out


def run(command: list, *, path: str, text: str, lang: str, role: str,
        cwd: str = "", timeout: float = DEFAULT_TIMEOUT,
        limit: int = DEFAULT_LIMIT, source: str = "external") -> list:
    """Запустить адаптер и вернуть проверенные записи.

    `command` — список argv, не строка: оболочка не участвует. Пустой stdout — пустой
    список, это законный ответ «адаптер не нашёл ничего».
    """
    if not command or not all(isinstance(x, str) and x for x in command):
        raise AdapterError("команда адаптера пуста")
    payload = json.dumps({"path": path, "text": text, "lang": lang, "role": role},
                         ensure_ascii=False)
    try:
        cp = subprocess.run(command, input=payload, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=cwd or None,
                            timeout=max(0.1, float(timeout)))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"адаптер не запустился: {exc}") from exc
    if cp.returncode != 0:
        raise AdapterError(f"адаптер вернул {cp.returncode}: {cp.stderr[-300:]}")
    out = _records(cp.stdout, max(1, int(limit)))
    for rec in out:
        rec.setdefault("file", path)
        rec.setdefault("src", source)
        rec.setdefault("conf", "EXTRACTED")
    return out
