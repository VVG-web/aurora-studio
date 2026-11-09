#!/usr/bin/env python3
"""part_227 — Code: справка индекса и Clean Room, список историй без терминала."""
from __future__ import annotations

import json

from harness import KIT, test


@test
def test_code_pack_list_refreshes_state_instead_of_running_a_command(_t):
    code = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    assert "async function refreshPack(ctx)" in code
    assert 'ctx.api("/api/code/pack?project="' in code
    assert 'runWatched("code:pack", ["--list"])' not in code
    assert "code.pack.list" in code


@test
def test_code_index_and_clean_room_explain_what_they_do(_t):
    code = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    for token in ("code.index.build_hint", "code.cleanroom.hint", "code.cleanroom.graphify"):
        assert token in code, token
    for lang in ("ru", "en"):
        data = json.loads((KIT / "cockpit/modules/code/i18n" / f"{lang}.json")
                          .read_text(encoding="utf-8"))
        for key in ("code.index.build_hint", "code.cleanroom.hint", "code.cleanroom.graphify"):
            assert data.get(key), (lang, key)


@test
def test_code_room_docs_name_kit_templates_and_graphify(_t):
    for rel in ("docs/code-room.md", "docs/en/code-room.md"):
        text = (KIT / rel).read_text(encoding="utf-8")
        assert "kit_path.txt" in text, rel
        assert "Graphify" in text, rel
