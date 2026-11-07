#!/usr/bin/env python3
"""part_212 — регрессия скорости раздела Code.

Первый ответ `/api/code` должен отдавать реестр сразу (`loading: true`) и не зависеть
от числа репозиториев; тяжёлые части (индекс, артефакты, статус клонов) считаются один
раз на состояние, а не по разу на репозиторий. Полный расчёт — в разумном бюджете.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
sys.path.insert(0, str(KIT / "cockpit"))
import aurora_cockpit as CK  # noqa: E402
import code_repos as CR  # noqa: E402
import code_sync as CSYNC  # noqa: E402


def _project_with_repos(tmp: Path, n: int) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    root = make_project(tmp)
    repos = {f"repo-{i}": {**CR.blank(), "url": f"https://gitlab.example.com/org/repo-{i}.git",
                            "provider": "gitlab"} for i in range(n)}
    if repos:
        config = root / "aurora.config.yaml"
        config.write_text(config.read_text(encoding="utf-8") + "\n"
                          + "\n".join(CR.block_lines(repos)) + "\n", encoding="utf-8")
    return root


def _clear(project: Path) -> None:
    key = str(project)
    with CK._CODE_STATE_LOCK:
        CK._CODE_STATE_CACHE.pop(key, None)
        CK._CODE_STATE_BUSY.pop(key, None)


@test
def test_code_first_response_is_lightweight_for_many_repositories(tmp):
    old = CK._code_state_bg
    CK._code_state_bg = lambda *_a, **_k: None
    try:
        for n in (0, 1, 10, 50):
            root = _project_with_repos(tmp / f"n{n}", n)
            _clear(root)
            started = time.perf_counter()
            st = CK.code_state(str(root))
            elapsed = time.perf_counter() - started
            _clear(root)
            assert st.get("loading") is True, (n, st.get("loading"))
            assert len(st.get("repos") or []) == n, (n, st.get("repos"))
            assert (st.get("index") or {}).get("repos") == [], (n, st.get("index"))
            assert (st.get("artifacts") or {}).get("kinds") == [], (n, st.get("artifacts"))
            assert elapsed < 1.5, (n, round(elapsed, 3))
    finally:
        CK._code_state_bg = old


@test
def test_code_fresh_cache_is_returned_without_background_work(tmp):
    root = _project_with_repos(tmp, 50)
    key = str(root)
    marker = {"repos": [{"name": "cached"}], "digest": "x", "loading": False}
    with CK._CODE_STATE_LOCK:
        CK._CODE_STATE_CACHE[key] = {"state": marker, "at": time.time()}
        CK._CODE_STATE_BUSY.pop(key, None)
    called = []
    old = CK._code_state_bg
    CK._code_state_bg = lambda *_a, **_k: called.append(1)
    try:
        started = time.perf_counter()
        got = CK.code_state(key)
        elapsed = time.perf_counter() - started
    finally:
        CK._code_state_bg = old
        _clear(root)
    assert got is marker and not called, (got, called)
    assert elapsed < 0.2, round(elapsed, 3)


@test
def test_full_state_does_not_repeat_expensive_work_per_repository(tmp):
    calls = {"index": 0, "artifacts": 0, "status": 0}
    old_index, old_art, old_status = CK.code_index_state, CK.code_artifacts_state, CSYNC.status
    CK.code_index_state = lambda _p: (calls.__setitem__("index", calls["index"] + 1)
                                      or {"repos": []})
    CK.code_artifacts_state = lambda _p, _lang="ru": (calls.__setitem__("artifacts", calls["artifacts"] + 1)
                                                      or {"kinds": [], "problems": [],
                                                          "digest": "", "overrides": {}})
    CSYNC.status = lambda _p: (calls.__setitem__("status", calls["status"] + 1)
                               or {"repos": [], "failed": ""})
    try:
        for n in (0, 10, 50):
            root = _project_with_repos(tmp / f"full{n}", n)
            st = CK._code_state_full(str(root))
            assert len(st["repos"]) == n, (n, len(st["repos"]))
            assert calls == {"index": 1, "artifacts": 1, "status": 1}, (n, calls)
            calls.update(index=0, artifacts=0, status=0)
    finally:
        CK.code_index_state, CK.code_artifacts_state, CSYNC.status = old_index, old_art, old_status


@test
def test_full_state_budget_with_fifty_repositories(tmp):
    root = _project_with_repos(tmp, 50)
    started = time.perf_counter()
    st = CK._code_state_full(str(root))
    elapsed = time.perf_counter() - started
    assert len(st["repos"]) == 50, len(st["repos"])
    assert len(st["groups"]["groups"]) == 0, st["groups"]
    assert elapsed < 3.0, round(elapsed, 3)
