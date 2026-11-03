#!/usr/bin/env python3
"""part_191 — контракт необязательного адаптера: JSON/JSONL, лимит, ошибки, таймаут."""
from __future__ import annotations

import sys

from harness import SCRIPTS, test

sys.path.insert(0, str(SCRIPTS))
from codeidx.adapters import external  # noqa: E402


def _cmd(code: str) -> list:
    return [sys.executable, "-c", code]


@test
def test_external_adapter_accepts_array_and_jsonl(_t):
    """Массив и JSONL дают записи с file/src/conf по умолчанию."""
    code = ("import json,sys; d=json.load(sys.stdin);"
            "print(json.dumps([{'rec':'symbol','name':'run','line_start':1}]))")
    rows = external.run(_cmd(code), path="src/a.py", text="x", lang="Python", role="src")
    assert rows == [{"rec": "symbol", "name": "run", "line_start": 1,
                     "file": "src/a.py", "src": "external", "conf": "EXTRACTED"}], rows

    code = ("import json,sys; d=json.load(sys.stdin);"
            "print(json.dumps({'rec':'route','path':'/x'}));"
            "print(json.dumps({'rec':'topic','name':'orders'}))")
    rows = external.run(_cmd(code), path="src/a.java", text="x", lang="Java", role="src")
    assert [r["rec"] for r in rows] == ["route", "topic"], rows
    assert all(r["file"] == "src/a.java" and r["src"] == "external" for r in rows), rows


@test
def test_external_adapter_rejects_bad_contract(_t):
    """Неизвестный вид, не-объект, лимит, код возврата и таймаут — AdapterError."""
    cases = [
        _cmd("print('{}')"),
        _cmd("print('{\"rec\":\"unknown\"}')"),
        _cmd("print('{\"rec\":\"symbol\"}'); print('{\"rec\":\"symbol\"}')"),
    ]
    for command in cases:
        try:
            external.run(command, path="a", text="x", lang="Python", role="src", limit=1)
        except external.AdapterError:
            continue
        raise AssertionError(f"плохой ответ принят: {command}")

    try:
        external.run(_cmd("import sys; sys.exit(3)"), path="a", text="x", lang="Python", role="src")
    except external.AdapterError:
        pass
    else:
        raise AssertionError("ненулевой код возврата принят")

    try:
        external.run(_cmd("import time; time.sleep(2)"), path="a", text="x",
                     lang="Python", role="src", timeout=0.1)
    except external.AdapterError:
        pass
    else:
        raise AssertionError("таймаут не поймали")


@test
def test_external_adapter_empty_stdout_is_empty_result(_t):
    """Пустой stdout — законный ответ «ничего не нашёл», а не ошибка."""
    assert external.run(_cmd("pass"), path="a", text="x", lang="Python", role="src") == []
