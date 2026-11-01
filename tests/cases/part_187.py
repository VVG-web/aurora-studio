#!/usr/bin/env python3
"""part_187 — MCP-инструменты кода (CR-13): поиск, символ, файл, границы и секреты."""
from __future__ import annotations

import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402
import aurora_mcp as MCP  # noqa: E402


def _config(root: Path, repos: dict, extra: str = "") -> None:
    path = root / "aurora.config.yaml"
    text = path.read_text(encoding="utf-8") + extra
    path.write_text(CR.write_block(text, repos), encoding="utf-8")


def _index(root: Path) -> None:
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root, expect_rc=0)


@test
def test_mcp_code_search_and_symbol(_t):
    """code_search и code_symbol читают индекс и называют файл и строку."""
    root = make_project(_t)
    repo = CF.build("order-intake", root / "Code" / "order-intake")
    _config(root, {"order-intake": {"url": CF.url_of(repo), "branch": "main",
                                    "provider": "generic", "models": True}})
    _index(root)
    text = MCP.t_code_search(str(root), {"query": "orders.accepted"})
    assert "topic" in text and "orders.accepted" in text, text
    sym = MCP.t_code_symbol(str(root), {"name": "OrderRequest"})
    assert "OrderRequest" in sym and "OrderRequest.java" in sym, sym


@test
def test_mcp_code_file_respects_model_permission_and_secrets(_t):
    """Файл не отдаётся без разрешения «Моделям»; с разрешением секрет маскируется."""
    root = make_project(_t)
    repo = CF.build("py-notify", root / "Code" / "py-notify")
    _config(root, {"py-notify": {"url": CF.url_of(repo), "branch": "main",
                                 "provider": "generic", "models": False}})
    _index(root)
    try:
        MCP.t_code_file(str(root), {"repo": "py-notify", "path": "app/main.py", "start": 1, "end": 5})
        raise AssertionError("файл отдан без разрешения «Моделям»")
    except MCP.ToolError as e:
        assert "Моделям" in str(e) or "разрешён" in str(e), e
    # разрешение включаем и добавляем секрет в отдельный файл
    token = "AKIA" + "1234567890ABCDEF"
    CF.commit(repo, "BENCH-1: secret", "2026-02-01T00:00:00+00:00",
              files={"src/config.py": 'TOKEN = "' + token + '"\n'})
    _config(root, {"py-notify": {"url": CF.url_of(repo), "branch": "main",
                                 "provider": "generic", "models": True}})
    _index(root)
    out = MCP.t_code_file(str(root), {"repo": "py-notify", "path": "src/config.py", "start": 1, "end": 5})
    assert "секрет скрыт" in out, out
    assert token not in out, "значение секрета ушло через MCP"
